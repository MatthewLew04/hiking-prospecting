#!/usr/bin/env python3
"""Propose Phase E known-item candidates from the live WS13 corpus.

The gap this fills: tests/fixtures/ws13_known_items.json needs 25 items and
carries exactly one -- the reviewed IGS IF0126 Lava Creek triple that WS12
already verified by hand. Without the other 24 there is no gate that would
notice a retrieval regression, and in particular nothing that would notice a
silently dead vector arm, because a missing ANN index produces no error at
all: the same rows come back from a sequential scan over 852,027 chunks, just
too late for the 30 s API Gateway deadline.

Runs in-VPC (via SSM on an nwmm-ws13 fleet node), read-only, against the
ws13_reader role. What it does:

  1. strata: one document per (admission_class, state, doc_type) combination.
     Under --balanced the candidates are drawn per class in a round robin --
     10,957 originals, 13,013 licensed-copies, 32,312 research-copies -- so
     the set spans all three rights prefixes instead of leaving whichever one
     the random stratum order under-drew untested;
  2. phrase: a verbatim, whitespace-normalised line from one of that
     document's chunks, long enough to identify a page, short enough to fit
     inside a 760-character excerpt, and near enough to the START of the chunk
     that the excerpt window can actually contain it;
  3. distinctiveness: the phrase is rejected unless phraseto_tsquery matches
     at most --max-matches documents corpus-wide. A phrase that appears in
     forty mine files cannot prove a specific document was retrieved;
  4. integrity: every candidate is run through
     tests/test_ws13_known_items.item_problems() before it is written, so the
     tool cannot emit an item that turns the offline suite red.

Every emitted item is verified=false and every question is a TEMPLATE. A human
reads the page, rewrites the question in their own words, and sets verified.
'complete' is never flipped by this tool. A generated question that repeats
the quote verbatim would prove only that the lexical arm works.

Rerun-safe: existing items are never rewritten, ids and (sha256, page, quote)
triples already present are never re-proposed, and --seed makes the stratum
sample deterministic, so a second run over an unchanged corpus proposes the
same candidates.

    # first pass, all three classes, write nothing
    ws13_gen_known_items.py --balanced --dry-run

    # top the set up to 25 and merge into the fixture
    ws13_gen_known_items.py --balanced

    # more coverage for one under-represented class, within the target
    ws13_gen_known_items.py --admission-class licensed-copies --count 4
"""
import argparse, json, os, re, sys, unicodedata
from datetime import date
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tests'))

# The offline gate is the authority on what a well-formed item is; duplicating
# its rules here is how the two drift apart.
from test_ws13_known_items import item_problems          # noqa: E402

DEFAULT_FIXTURE = ROOT / 'tests' / 'fixtures' / 'ws13_known_items.json'
ADMISSION_CLASSES = ('originals', 'licensed-copies', 'research-copies')
TARGET_COUNT = 25
# Matches tests/test_ws13_known_items.py. A quote has to fit inside the
# default 760-character excerpt with room for the centring window, and be long
# enough that it is not a heading fragment shared by hundreds of mine files.
MIN_QUOTE_CHARS = 40
MAX_QUOTE_CHARS = 240
MIN_QUOTE_WORDS = 6
# The live gate can only ever see the LEADING part of a chunk.
# infra/ws13_query_lambda.excerpt() centres its window on the first query-term
# match and terms_of() keeps every token over one character, so a common word
# from a natural-language question ('the', 'and', 'county') matches at about
# offset 0 in any prose chunk and start = max(0, center - maximum//3)
# collapses to 0. pipelines/ws13_worker.CHUNK_CHARS is 3000 and 851,619 chunks
# cover 760,043 pages, so chunks routinely run well past 760 characters: a
# phrase picked at offset 2,000 would fail judge() with 'the quote is in none
# of the hits' on every run, forever, even when retrieval returned exactly the
# right chunk.
EXCERPT_CHARS = 760
EXCERPT_MARGIN_CHARS = 20        # the window's leading/trailing ellipsis
MAX_PHRASE_END = EXCERPT_CHARS - EXCERPT_MARGIN_CHARS
# OCR garbage is the failure mode here: a line of misread characters passes a
# length check and then never matches anything. Require most of the line to be
# letters, digits, spaces or ordinary punctuation.
MIN_CLEAN_RATIO = 0.90
CLEAN_RE = re.compile(r"[A-Za-z0-9 ,.;:'/()\-]")
DIGIT_RUN_RE = re.compile(r'\d{9,}')
CHUNKS_PER_DOC = 12
# Each rejected phrase costs one phraseto_tsquery probe against 852,027 rows.
# Phrases are tried best-first, so the accepted one is normally in the first
# two or three; this caps a pathological page at bounded work.
MAX_PHRASE_PROBES = 8
STATEMENT_TIMEOUT_MS = 60000

STRATA_SQL = """
SELECT sha256, admission_class, state, doc_type, title, doc_date
  FROM (SELECT d.sha256, d.admission_class, d.state, d.doc_type, d.title,
               d.doc_date,
               row_number() OVER (
                   PARTITION BY d.admission_class, coalesce(d.state, ''),
                                coalesce(d.doc_type, '')
                   ORDER BY md5(d.sha256 || %s)) AS stratum_rank
          FROM ws13_documents d
         WHERE d.admission_class = ANY(%s){extra}) ranked
 WHERE stratum_rank = 1
 ORDER BY md5(sha256 || %s)
 LIMIT %s
"""

CHUNKS_SQL = """
SELECT c.page, c.ordinal, c.text
  FROM ws13_chunks c
 WHERE c.sha256 = %s AND c.text IS NOT NULL AND c.text <> ''
 ORDER BY c.page, c.ordinal
 LIMIT %s
"""

# LIMIT inside the subquery caps the work: the answer only has to distinguish
# "at most max_matches" from "more", never count all 852,027 chunks.
DISTINCT_DOCS_SQL = """
SELECT count(*)
  FROM (SELECT DISTINCT sha256
          FROM ws13_chunks
         WHERE tsv @@ phraseto_tsquery('english', %s)
         LIMIT %s) probe
"""

SELF_MATCH_SQL = """
SELECT EXISTS (SELECT 1
                 FROM ws13_chunks
                WHERE sha256 = %s AND page = %s
                  AND tsv @@ phraseto_tsquery('english', %s))
"""

REMINDER = """
NOT DONE YET. Every item above is verified=false and every question is a
template. Before "complete" can become true, for each item:

  1. open the page -- viewer_key resolves it -- and read it;
  2. confirm the quote is on THAT page of THAT document, verbatim;
  3. rewrite the question the way a geologist would actually ask it. A
     question built out of the quote's own words only proves the lexical arm
     works; the vector arm is what a paraphrase tests, and a silently dead
     vector arm is the failure this whole gate exists to catch. This is
     ENFORCED: an item whose question still repeats more than five of the
     quote's words in order may not be marked verified;
  4. set "verified": true on the item.

Only when all 25 items are present and verified does "complete" flip to true.
tests/test_ws13_known_items.py:require_complete() is the gate that enforces
it, and tools/ws13_live_known_items.py refuses to certify a cutover until it
returns clean.
"""


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--dsn', default=os.environ.get('WS13_DB_DSN'),
                        help='libpq URI for the ws13_reader role')
    parser.add_argument('--balanced', action='store_true',
                        help='spread candidates over all three admission classes')
    parser.add_argument('--admission-class', action='append', default=[],
                        choices=list(ADMISSION_CLASSES),
                        help='restrict to this rights prefix (repeatable)')
    parser.add_argument('--state', action='append', default=[],
                        help='restrict to this two-letter state (repeatable)')
    parser.add_argument('--doc-type', action='append', default=[],
                        help='restrict to this doc_type (repeatable)')
    parser.add_argument('--count', type=int,
                        help='how many candidates to propose '
                             '(default: enough to reach target_count)')
    parser.add_argument('--fixture', default=str(DEFAULT_FIXTURE),
                        help='fixture to merge into')
    parser.add_argument('--out', help='write here instead of --fixture')
    parser.add_argument('--seed', default='ws13-known-items',
                        help='mixed into the stratum ordering; the same seed '
                             'over an unchanged corpus proposes the same items')
    parser.add_argument('--max-matches', type=int, default=3,
                        help='reject a phrase matching more documents than this')
    parser.add_argument('--strata-limit', type=int, default=400,
                        help='documents to consider before phrase selection')
    parser.add_argument('--dry-run', action='store_true',
                        help='print the candidates and write nothing')
    args = parser.parse_args(argv)
    if not (args.balanced or args.admission_class or args.state
            or args.doc_type):
        parser.error('a selector is required: --balanced, --admission-class, '
                     '--state or --doc-type')
    if not args.dsn:
        parser.error('--dsn is required (or set WS13_DB_DSN)')
    return args


def connect(dsn):
    """Read-only connection. default_transaction_read_only is a guard, not a
    formality: this tool runs against production and must not be able to write
    even by accident."""
    conn = psycopg.connect(dsn, autocommit=True)
    conn.execute("SELECT set_config('default_transaction_read_only', 'on', false)")
    conn.execute("SELECT set_config('statement_timeout', %s, false)",
                 (str(STATEMENT_TIMEOUT_MS),))
    return conn


def load_fixture(path):
    path = Path(path)
    if not path.exists():
        return {'schema_version': 1, 'dataset': 'ws13-known-items',
                'target_count': TARGET_COUNT, 'complete': False, 'items': []}
    with open(path, encoding='utf-8') as handle:
        return json.load(handle)


def normalize(text):
    """Whitespace-normalised, the way infra/ws13_query_lambda.excerpt() is."""
    return re.sub(r'\s+', ' ', text or '').strip()


def clean_ratio(line):
    if not line:
        return 0.0
    return sum(1 for char in line if CLEAN_RE.match(char)) / len(line)


def specificity(phrase):
    """How likely a phrase is to name one document rather than a genre.

    Long content words and a four-digit year are what separate "exposed a
    silver-lead shoot averaging fourteen ounces per ton" from "From USGS
    Mineral Resources Data System", which is boilerplate on thousands of MRDS
    records. The corpus-wide count in is_distinctive() is the real filter;
    this only decides which phrase to spend that query on first.
    """
    words = {word.lower().strip(",.;:'()") for word in phrase.split(' ')}
    long_words = sum(1 for word in words if len(word) >= 6)
    year = 2 if re.search(r'\b1[89]\d{2}\b|\b20[0-2]\d\b', phrase) else 0
    number = 1 if re.search(r'\d', phrase) else 0
    return long_words + year + number + len(phrase) / 1000.0


def candidate_phrases(text):
    """(phrase, offset) pairs from one chunk that could identify a page.

    Split on line breaks first and sentence ends second: OCR output keeps the
    page's line structure, and a line is what a reader can find again on the
    page when they verify the item.

    The offset is the phrase's start in the WHITESPACE-NORMALISED chunk --
    the same text infra/ws13_query_lambda.excerpt() windows -- and anything
    reaching past MAX_PHRASE_END is dropped here rather than becoming an item
    the live gate can never pass. Best first, by specificity only; position is
    a hard filter, not a ranking, so a rerun still picks the same quote.
    """
    flat = normalize(text)
    phrases = {}
    for raw_line in re.split(r'[\r\n]+', text or ''):
        for part in re.split(r'(?<=[.;:])\s+', raw_line):
            phrase = normalize(unicodedata.normalize('NFKC', part))
            if not (MIN_QUOTE_CHARS <= len(phrase) <= MAX_QUOTE_CHARS):
                continue
            words = phrase.split(' ')
            if len(words) < MIN_QUOTE_WORDS:
                continue
            if clean_ratio(phrase) < MIN_CLEAN_RATIO:
                continue
            if DIGIT_RUN_RE.search(phrase):
                continue        # a coordinate table, not a sentence
            if not re.search(r'[A-Za-z]{4,}', phrase):
                continue
            if sum(char.isalpha() for char in phrase) < len(phrase) // 2:
                continue
            start = flat.find(phrase)
            if start < 0:
                # NFKC rewrote the line (a ligature, a full-width digit). The
                # retrieval excerpt collapses whitespace and nothing else, so
                # the stored quote would not match what it returns.
                continue
            if start + len(phrase) > MAX_PHRASE_END:
                continue
            phrases.setdefault(phrase, start)
    # Deterministic: the phrase text breaks ties so a rerun over an unchanged
    # chunk picks the same quote.
    return sorted(phrases.items(),
                  key=lambda item: (-specificity(item[0]), item[0]))


def is_distinctive(conn, phrase, sha256, page, max_matches):
    """True when the phrase names few enough documents AND names this one."""
    matches = conn.execute(DISTINCT_DOCS_SQL,
                           (phrase, max_matches + 1)).fetchone()[0]
    if matches == 0 or matches > max_matches:
        return False
    return bool(conn.execute(SELF_MATCH_SQL, (sha256, page, phrase)).fetchone()[0])


def strata(conn, args):
    extra, params = '', [args.seed, list(args.admission_class or
                                         ADMISSION_CLASSES)]
    if args.state:
        extra += ' AND d.state = ANY(%s)'
        params.append(list(args.state))
    if args.doc_type:
        extra += ' AND d.doc_type = ANY(%s)'
        params.append(list(args.doc_type))
    sql = STRATA_SQL.format(extra=extra)
    params.extend([args.seed, args.strata_limit])
    return conn.execute(sql, params).fetchall()


def slug(admission_class, state, sha256, page):
    short = {'originals': 'orig', 'licensed-copies': 'lic',
             'research-copies': 'res'}.get(admission_class, 'unk')
    # 12 hex characters, not 8: 8 collides across a 56,282-document corpus
    # often enough to silently drop a good candidate.
    return f'{short}-{(state or "xx").lower()}-{sha256[:12]}-p{page}'


def question_for(title, phrase):
    """A TEMPLATE question, kept free of placeholder markers on purpose.

    A human rewrites this before setting verified; the item's provenance says
    so and the reminder printed at the end says so. It carries no 'TODO' text
    because tests/test_ws13_known_items.py rejects placeholder markers, and a
    generated-but-unverified item still has to pass the integrity gate.
    """
    words, used = [], 0
    for word in phrase.split(' '):
        if len(word) <= 3:
            continue
        if used + len(word) + 1 > 80:
            break                       # whole words only; never mid-word
        words.append(word.strip(',.;:'))
        used += len(word) + 1
    name = normalize(title) or 'this document'
    return f'In "{name}", what is recorded about {" ".join(words)}?'


def propose(conn, args, used_shas, used_triples, used_ids, wanted):
    """Candidate items, at most one per document, at most `wanted` total."""
    proposed = []
    for sha256, admission_class, state, doc_type, title, doc_date in strata(
            conn, args):
        if len(proposed) >= wanted:
            break
        if sha256 in used_shas:
            continue
        chunks = conn.execute(CHUNKS_SQL, (sha256, CHUNKS_PER_DOC)).fetchall()
        picked, probes = None, 0
        for page, ordinal, text in chunks:
            for phrase, offset in candidate_phrases(text):
                triple = (sha256, page, phrase)
                if triple in used_triples:
                    continue
                if probes >= MAX_PHRASE_PROBES:
                    break
                probes += 1
                if not is_distinctive(conn, phrase, sha256, page,
                                      args.max_matches):
                    continue
                picked = (page, ordinal, phrase, offset)
                break
            if picked or probes >= MAX_PHRASE_PROBES:
                break
        if not picked:
            continue
        page, ordinal, phrase, offset = picked
        identifier = slug(admission_class, state, sha256, page)
        if identifier in used_ids:
            continue
        candidate = {
            'id': identifier,
            'question': question_for(title, phrase),
            'sha256': sha256,
            'page': int(page),
            'quote': phrase,
            'expect_vector_source': True,
            'admission_class': admission_class,
            'sidecar_sha256': None,
            'verified': False,
            'provenance': (
                f'generated by tools/ws13_gen_known_items.py (seed '
                f'{args.seed!r}) from ws13_chunks id-ordered chunk '
                f'page {page} ordinal {ordinal} of ws13_documents '
                f'{sha256[:12]}, at offset {offset} of the '
                f'whitespace-normalised chunk; admission_class '
                f'{admission_class}, state '
                f'{state or "unknown"}, doc_type {doc_type or "unknown"}, '
                f'doc_date {doc_date or "unknown"}; phrase matched at most '
                f'{args.max_matches} documents corpus-wide. Question is a '
                f'template and the item is UNVERIFIED: a human must read the '
                f'page, confirm the quote and rewrite the question.'),
        }
        # The offline gate decides what ships. A candidate it would reject --
        # a quote OCR'd as a bare 'XXX', a page of 0 -- must never reach the
        # fixture, where it would turn CI red for an item nobody chose.
        problems = item_problems(candidate)
        if problems:
            print(f'rejected {identifier}: {"; ".join(problems)}',
                  file=sys.stderr)
            continue
        used_ids.add(identifier)
        used_shas.add(sha256)
        used_triples.add((sha256, page, phrase))
        proposed.append(candidate)
    return proposed


def propose_balanced(conn, args, used_shas, used_triples, used_ids, wanted):
    """Round-robin over the three rights prefixes, then top up.

    --balanced used to pass all three classes into one `= ANY(%s)` predicate
    and let `ORDER BY md5(sha256 || seed) LIMIT 400` draw the partitions at
    random. Nothing then held a per-class quota, so a run could legitimately
    return 24 research-copies and zero licensed-copies -- leaving CC BY-NC-SA,
    the prefix carrying the share-alike and non-commercial obligations, wholly
    untested by the gate while the docstring claimed otherwise.
    """
    quotas = {name: wanted // len(ADMISSION_CLASSES)
              for name in ADMISSION_CLASSES}
    for index in range(wanted % len(ADMISSION_CLASSES)):
        quotas[ADMISSION_CLASSES[index]] += 1
    proposed = []
    for name in ADMISSION_CLASSES:
        if quotas[name] <= 0:
            continue
        scoped = argparse.Namespace(**vars(args))
        scoped.admission_class = [name]
        proposed.extend(propose(conn, scoped, used_shas, used_triples,
                                used_ids, quotas[name]))
    if len(proposed) < wanted:
        # A class that cannot fill its quota must not shrink the set; the
        # top-up runs unrestricted and skips everything already taken.
        proposed.extend(propose(conn, args, used_shas, used_triples, used_ids,
                                wanted - len(proposed)))
    return proposed


def report(items, fixture, wanted):
    for item in items:
        print(f'  {item["id"]}  p{item["page"]}  {item["sha256"][:12]}')
        print(f'      quote: {item["quote"][:76]}')
    present = len(fixture['items'])
    print(f'\nproposed {len(items)} of {wanted} requested; fixture would hold '
          f'{present + len(items)} of {fixture.get("target_count", TARGET_COUNT)}')
    # Per-class counts, because the cutover gate refuses a set that misses a
    # rights prefix and an operator should see the gap before CI does.
    merged = list(fixture.get('items') or []) + list(items)
    counts = {name: sum(1 for item in merged
                        if item.get('admission_class') == name)
              for name in ADMISSION_CLASSES}
    undeclared = sum(1 for item in merged if not item.get('admission_class'))
    print('by admission_class: '
          + ', '.join(f'{name} {counts[name]}' for name in ADMISSION_CLASSES)
          + f', undeclared {undeclared}')


def main(argv=None):
    args = parse_args(argv)
    fixture = load_fixture(args.fixture)
    existing = fixture.get('items') or []
    used_ids = {str(item.get('id')) for item in existing}
    used_shas = {str(item.get('sha256')) for item in existing}
    used_triples = {(str(item.get('sha256')), item.get('page'),
                     normalize(item.get('quote'))) for item in existing}
    target = int(fixture.get('target_count') or TARGET_COUNT)
    room = max(0, target - len(existing))
    # --count asks for FEWER, never for more: the offline gate rejects a
    # fixture over its target ('29 items exceed the target of 25'), so an
    # uncapped top-up run wrote a set that could only be repaired by hand.
    wanted = min(args.count, room) if args.count is not None else room
    if wanted <= 0:
        print(f'fixture already holds {len(existing)} of {target} items; '
              'nothing to propose. Raise target_count in the fixture, or '
              'remove an item, to make room')
        return 0
    if args.count is not None and args.count > room:
        print(f'--count {args.count} clamped to {room}: the set holds '
              f'{len(existing)} of {target} and may not exceed its target')

    conn = connect(args.dsn)
    try:
        if args.balanced and not args.admission_class:
            proposed = propose_balanced(conn, args, used_shas, used_triples,
                                        used_ids, wanted)
        else:
            proposed = propose(conn, args, used_shas, used_triples, used_ids,
                               wanted)
    finally:
        conn.close()

    report(proposed, fixture, wanted)
    if not proposed:
        print('no candidate survived the distinctiveness filter; widen '
              '--max-matches or the selector', file=sys.stderr)
        return 1

    merged = existing + proposed
    if len(merged) > target:
        # Belt and braces behind the --count clamp: nothing this tool writes
        # may leave the fixture in a state the offline suite rejects.
        print(f'refusing to write {len(merged)} items over a target of '
              f'{target}', file=sys.stderr)
        return 1
    fixture['items'] = merged
    # Never flipped here. A generated item is a proposal, not a verified one.
    fixture['complete'] = False
    fixture['generated'] = date.today().isoformat()
    if args.dry_run:
        print('\n--dry-run: nothing written')
        print(REMINDER)
        return 0 if len(proposed) >= wanted else 1

    out = Path(args.out or args.fixture)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(out.suffix + '.pending')
    with open(tmp, 'w', encoding='utf-8') as handle:
        json.dump(fixture, handle, indent=2, ensure_ascii=False)
        handle.write('\n')
    os.replace(tmp, out)
    print(f'wrote {out}')
    print(REMINDER)
    # Fail closed: an operator must see that the set is still short rather
    # than read "wrote ..." and assume the gate is ready.
    return 0 if len(proposed) >= wanted else 1


if __name__ == '__main__':
    raise SystemExit(main())
