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

  1. strata: --per-stratum documents per (admission_class, state, doc_type)
     combination, breadth before depth. Under --balanced the candidates are
     drawn per class in a round robin -- 10,957 originals, 13,013
     licensed-copies, 32,312 research-copies -- so the set spans all three
     rights prefixes instead of leaving whichever one the random stratum order
     under-drew untested;
  2. phrase: a verbatim, whitespace-normalised line from one of that
     document's chunks -- sampled across the whole document, not off its front
     matter -- long enough to identify a page, short enough to fit inside a
     760-character excerpt, near enough to the START of the chunk that the
     excerpt window can actually contain it, and made of tokens that look like
     WORDS rather than legible OCR debris, since a human has to read the page,
     confirm the quote and paraphrase it;
  3. distinctiveness: the phrase is rejected unless phraseto_tsquery matches
     at most --max-matches documents corpus-wide. A phrase that appears in
     forty mine files cannot prove a specific document was retrieved;
  4. integrity: every candidate is run through
     tests/test_ws13_known_items.item_problems() before it is written, so the
     tool cannot emit an item that turns the offline suite red;
  5. instrumentation: every step above counts what it rejected and why, and
     the summary prints the funnel. A run that proposes 3 of 24 is not
     actionable without it -- the four stages fail for entirely different
     reasons and only the counts say which one is binding.

Every emitted item is verified=false and every question is a TEMPLATE. A human
reads the page, rewrites the question in their own words, and sets verified.
'complete' is never flipped by this tool. A generated question that repeats
the quote verbatim would prove only that the lexical arm works.

Rerun-safe: existing items are never rewritten, ids and (sha256, page, quote)
triples already present are never re-proposed, and --seed makes both the
stratum sample and the chunk sample within a document deterministic, so a
second run over an unchanged corpus proposes the same candidates. Every PHRASE
filter between those samples and the offline gate is a pure function of the
text. The gate itself is not: item_problems() ends in sidecar_problems(), which
reads $WS13_SIDECAR_DIR and stats the filesystem, so a host carrying a sidecar
for a page the sampler happens to draw can reject a candidate another host
keeps. It only ever rejects, and no sidecar is checked in today, so the
exception is currently inert -- but it is an exception, not a rounding error.

    # first pass, all three classes, write nothing
    ws13_gen_known_items.py --balanced --dry-run

    # top the set up to 25 and merge into the fixture
    ws13_gen_known_items.py --balanced

    # more coverage for one under-represented class, within the target
    ws13_gen_known_items.py --admission-class licensed-copies --count 4
"""
import argparse, collections, json, os, re, sys, unicodedata
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
#
# This is a CHARACTER LEGALITY test and nothing more. It scored the measured
# candidate
#     Ii'ille<l Out anu )Iailt*d to rbc ltfit:e of "'tate IDt-:fJC:' *to1*
# at 0.919 and admitted it, because every character in it is one a document
# could legitimately contain; only the arrangement is impossible. Word shape,
# below, is what rejects that line. Keep this check anyway: it is one pass over
# the string and it is what catches a line of box-drawing characters or a
# dingbat rule, which word shape scores as no tokens at all.
MIN_CLEAN_RATIO = 0.90
CLEAN_RE = re.compile(r"[A-Za-z0-9 ,.;:'/()\-]")
DIGIT_RUN_RE = re.compile(r'\d{9,}')

# --- word shape -------------------------------------------------------------
# Why a second, different measure exists. A human cannot verify "this quote is
# on that page, verbatim" for mangled text, and step 3 of the workflow in
# REMINDER is to REWRITE THE QUESTION AS A PARAPHRASE -- which cannot be done
# at all for text that is not words. item_problems() then refuses to let a
# verified item repeat more than five of the quote's words in order, so a
# garbage quote produces an item that is impossible to complete: the operator
# can neither paraphrase it nor keep the template. It is worse than no
# candidate, because it consumes one of only 25 slots and a review cycle.
#
# So: character legality above, word shape here. A token is judged on shape
# alone -- no lexicon -- and the phrase is scored on the fraction of judgeable
# tokens that look like words. It is a ratio, not a per-token veto, because
# real lines do contain odd tokens: 'lengths' trips the consonant-run rule
# (NGTHS), and that should not cost a page its candidate.
#
# What a no-lexicon rule cannot do, since the first draft of this comment
# claimed it could: it cannot tell 'pct' and 'lbs' from 'rbc' and 'nnd'. All
# four are three lowercase consonants; the first two are units this corpus
# writes constantly and the last two are misreads, and only a dictionary
# separates them. They are counted as junk and the ratio is what absorbs them,
# so a line carrying three or four of them can still fall below the floor.
# That is a known, measured cost, not a case the filter handles.
#
# WHAT IT DOES NOT CATCH, stated plainly: substitution OCR, where a misread
# letter yields a shape-plausible non-word. The line
#     tbe Sccrctary of tlic Intcrior, Wasbington, D. C., rcport ou minc
# scores 0.889 and is admitted -- 'tbe', 'tlic' and 'rcport' all have vowels,
# ordinary consonant runs and consistent case. Only a lexicon separates those
# from real words, and a lexicon would reject the domain vocabulary this corpus
# is made of. This filter catches STRUCTURAL failure -- punctuation sprayed
# into words, vowel-less runs, case and digit artifacts -- and the human review
# step remains the backstop for the rest.
VOWELS = set('AEIOUYaeiouy')
# A word token: letters, with internal apostrophes or hyphens joining letter
# runs. 'silver-lead' and "d'Alene" are words; 'ltfit:e' and "Ii'ille<l" are
# not, because a colon or an angle bracket inside a token is a scanner artifact
# in every document this corpus holds.
#
# The class is ASCII and the token is DIACRITIC-FOLDED before it is matched
# (fold_token). Matched raw, this rule made every accented letter junk, which
# is the corpus's own vocabulary and not debris: 'Cañon', 'Cañada', 'Peña' and
# 'Río' are pervasive in western US mining files and older Idaho documents
# spell it Coeur d'Alène. Four such tokens in one line scored 0.692 and the
# line was rejected.
WORD_TOKEN_RE = re.compile(r"^[A-Za-z]+(?:['-][A-Za-z]+)*$")
# Five, not four. Four rejects 'rights' and 'heights' (GHTS) -- and mineral
# rights is core vocabulary here -- while five still rejects 'Sccrctary'
# (SCCRCT). Five costs 'lengths' (NGTHS), which the ratio absorbs.
CONSONANT_RUN_RE = re.compile(r'[^AEIOUYaeiouy]{5,}')
# A digit BETWEEN two letters: 'Nan1e', 'clain1ant', the 1/l and 0/O
# substitutions that scanned type produces constantly. A digit at a token edge
# is left alone, because that is how real references are written -- T12N, No.3,
# SiO2, 400-foot. The cost of the rule is 'H2O' and 'T1N', which it calls
# junk; both are single tokens the ratio absorbs.
LETTER_DIGIT_RE = re.compile(r'[A-Za-z]\d[A-Za-z]')
MC_PREFIX_RE = re.compile(r'^(?:Mc|Mac)(?=[A-Z])')
# Two or more Titlecase fragments run together, which is a NAME and not broken
# case: 'DeLamar' (an Owyhee County silver mine, so a place name in this
# corpus's own region), 'DeWitt', 'LaGrange', 'VanZandt'. The interior-case
# rule called all four junk and four of them in one line scored 0.667. Each
# capital needs a lowercase RUN after it, which is exactly what keeps 'IDt' and
# 'PbS' out of this exemption -- they have no such run.
CAMEL_NAME_RE = re.compile(r'^(?:[A-Z][a-z]+){2,}$')
EDGE_PUNCT_RE = re.compile(r'^[^A-Za-z0-9]+|[^A-Za-z0-9]+$')
WORD, JUNK, SKIP, SINGLE, STRAY = 'word', 'junk', 'skip', 'single', 'stray'
# A capitalised token of at most four letters carrying no vowel is an
# abbreviation or a symbol, not a misread word: 'Twp', 'Mtn', 'Mts', 'Crk',
# 'Spgs', 'Bldg', the compass points 'NNW' and 'WSW', the mill reagent 'KCN'.
# Without this exemption a line of them scored 0.667-0.714 and was rejected.
# Lowercase tokens are NOT exempt, and the cost is stated in the header: 'pct'
# and 'lbs' stay junk, because nothing but a dictionary tells them from 'rbc'.
# The exemption is paid for in the other direction too -- a four-letter
# capitalised misread ('Tll' for 'Till') now reads as a word, one token.
ABBREV_MAX_LETTERS = 4
# A short token whose case is broken in neither direction is ABSTAINED on
# rather than condemned. 'PbS', 'ZnS', 'CaO' and 'HCl' have the same shape as
# 'IDt', and no rule without an element table separates a formula from a
# three-character scanner fragment, so counting either way is a guess dressed
# as a measurement. Abstaining is also what the digit rule already does one
# character along: 'SiO2' is SKIP, and 'PbS' differing from it only by the
# absence of a digit was an inconsistency, one that cost a mineralogy line
# carrying three formulae its candidate at 0.727. Length is the whole limit:
# above four characters a case-broken token is not a formula and stays junk.
MIXED_CASE_MAX = 4
# Single letters that stand on their own. Every UPPERCASE single letter is an
# abbreviation or an initial in this corpus -- 'T. 12 N., R. 4 E.' closes a
# PLSS description, 'U S G S' and 'U S B M' name the agencies that wrote half
# these files -- and these two are the lowercase exceptions: the article, and
# the 'x' of "a 6 x 8 raise". Any other lowercase single letter is the OCR of a
# ruled line, and the cost of that is an enumerated '(b)' or '(c)' label.
SINGLE_LETTER_WORDS = frozenset('ax')
# Measured over the calibration table in tests/test_ws13_gen_known_items.py,
# and these are the table's actual endpoints -- the numbers this comment
# carried before (0.800 for 'an X-ray/H2O line', 0.571) were in no table and
# overstated the headroom by about 40 percent. The worst real line scores
# 0.846: an assay line carrying 'pct' and 'lbs', the lowercase vowel-less
# abbreviations no lexicon-free rule tells from 'rbc'. The best garbage scores
# 0.667: a ruled form read as single letters. 0.75 sits 0.096 above the one
# and 0.096 below the other, which is as close to the middle of that gap as a
# round number gets. Raising it to 0.85 would take that real line; lowering it
# to 0.65 would admit the form garbage. The margin is thin enough that a new
# shape rule has to re-measure both ends, which is what
# test_the_floor_sits_in_the_gap_and_not_at_its_edge exists to force.
MIN_WORD_SHAPE_RATIO = 0.75
# A ratio over one or two tokens says nothing. MIN_QUOTE_WORDS counts
# whitespace tokens, of which a table line's are mostly numbers, so a phrase
# also has to carry four tokens that are actually words.
MIN_WORD_TOKENS = 4

CHUNKS_PER_DOC = 12
# pipelines/ws13_worker.CHUNK_CHARS. Copied, not imported: importing the
# worker pulls boto3 and the OCR stack onto a host that only needs psycopg.
# It is printed in the funnel header and used in no comparison, so a drift
# from the worker misleads a reader and changes no behaviour -- which is
# exactly why it is stated as a number and attributed to its owner.
CHUNK_CHARS_HINT = 3000
# Each rejected phrase costs one phraseto_tsquery probe against 852,027 rows.
#
# Was 8, and the probes were spent best-first WITHIN a chunk, so a text-dense
# document was examined on its first page only and pages 2..12 were fetched and
# never read. Probes are now interleaved across the sampled chunks -- best line
# of every chunk, then second-best of every chunk -- and the budget is set to
# CHUNKS_PER_DOC so that every sampled chunk gets exactly one probe before any
# chunk gets a second. Eight probes on one page are eight draws from a single
# distribution; twelve probes on twelve pages are twelve independent chances at
# a page whose text is not boilerplate, which is the whole difficulty
# is_distinctive() poses.
MAX_PHRASE_PROBES = CHUNKS_PER_DOC
# How deep to go inside one (admission_class, state, doc_type) partition.
#
# What is MEASURED: a --balanced --strata-limit 400 run proposed 3 of 24, split
# licensed-copies 1 / research-copies 2 / originals 0, and
# test_ws13_known_items.require_complete() blocks a cutover on that zero alone,
# whatever the total. What is NOT measured, and what an earlier draft of this
# comment asserted anyway: which stage the other 21 died at, and how many
# (state, doc_type) partitions each class actually has. Nothing has ever
# printed a partition count -- WS13-RETRIEVAL.md records class totals and
# nothing finer -- and the claim that 'originals' is the state-portal harvest
# was also the wrong class: infra/docs_lambda.py attributes the state-archive
# set to research-copies. The funnel's 'strata rows by scope' line is what will
# answer this on the next live run, and it is being added in the same change,
# which is the honest measure of how much was known when this default was set.
#
# What is ARGUABLE from the code alone, and is the whole case for the knob:
# STRATA_SQL keeps stratum_rank <= this, so a class returns min(--strata-limit,
# partitions x per-stratum) rows. At the depth of 1 this ran at, the row count
# IS the partition count, so a class holding few (state, doc_type) combinations
# offers few documents however many it holds -- and --strata-limit cannot
# reach that, being a CEILING such a class never touches. Depth is therefore
# the only knob that CAN add rows to a narrow class. Whether 'originals' is
# such a class is exactly what nobody has measured.
#
# 16 is deliberately generous and costs a wide class nothing, because the outer
# ORDER BY takes stratum_rank first: every partition is drawn at depth 1 before
# any partition is drawn at depth 2, so depth is a fallback that only engages
# once breadth runs out. A class with 400+ partitions sees the same rows it saw
# before; a class with 9 sees 144 instead of 9.
#
# And 144 rows is not a promised 8 candidates. The measured baseline accepted 3
# documents out of the hundreds each scoped pass walked, well under 1 percent;
# filling a quota of 8 from 144 rows needs better than 5 percent, an order of
# magnitude the chunk resampling and the probe interleave have to find while
# the word-shape filter added alongside them pushes the other way on exactly
# the scanned classes. So: this raises a ceiling that was provably in the way
# for a narrow class. It is not a demonstration that the next run clears
# require_complete().
DEFAULT_PER_STRATUM = 16
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
 WHERE stratum_rank <= %s
 ORDER BY stratum_rank, md5(sha256 || %s)
 LIMIT %s
"""

# NOT ORDER BY c.page, c.ordinal. That took the first CHUNKS_PER_DOC chunks of
# the document -- front matter -- and MAX_PHRASE_END then restricted every
# probe to the top quarter of each of those. specificity() ranks a four-digit
# year and long words first, which on a cover page is the title/date/letterhead
# block: so every probe the tool spent was aimed at the one region of a
# document that its siblings from the same portal share word for word, and
# is_distinctive() rejected all of it at --max-matches 3, correctly. The
# sampler was showing the filter nothing but boilerplate.
#
# md5 over the chunk's own key plus the seed spreads the sample over the whole
# document instead, and stays deterministic: the same seed over an unchanged
# corpus draws the same chunks, which is what rerun-safety rests on. Front
# matter is still drawn, at its honest share of the document's pages.
#
# The ':' separators are load-bearing and the trailing page/ordinal is the
# belt to their braces. Concatenated bare, page || ordinal is not injective --
# (page 1, ordinal 10) and (page 11, ordinal 0) hash identically, as do
# (2, 12) and (21, 2) -- and chunk_pages() restarts `ordinal` at 0 on every
# page, so both members of such a pair are real rows of ONE document whenever
# a page carries eleven chunks (~26k characters at CHUNK_CHARS 3000). A tie
# leaves ORDER BY without a total order, Postgres may return either row first
# depending on plan shape or physical order after a VACUUM, and at LIMIT 12 a
# tie at the boundary swaps a whole chunk in or out of the sample. That is a
# rerun proposing a different quote for the same document, which is the one
# thing the seed promises cannot happen.
CHUNKS_SQL = """
SELECT c.page, c.ordinal, c.text
  FROM ws13_chunks c
 WHERE c.sha256 = %s AND c.text IS NOT NULL AND c.text <> ''
 ORDER BY md5(c.sha256 || ':' || c.page::text || ':' || c.ordinal::text
              || ':' || %s), c.page, c.ordinal
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


# The funnel, in the order a candidate walks it. Printed in full, zeros
# included: a missing line and a zero are the same thing to a reader, and the
# zero is the more useful of the two -- "phrase.past_excerpt_end 0" is what
# rules MAX_PHRASE_END out as the binding constraint.
FUNNEL_SECTIONS = (
    ('documents', (
        ('doc.strata', 'drawn by the strata query'),
        ('doc.not_reached', 'never examined (the quota filled first)'),
        ('doc.examined', 'examined'),
        ('doc.already_taken', 'skipped: already carries a fixture item'),
        ('doc.rewalked', 'skipped: an earlier pass of this run examined it'),
        ('doc.no_chunks', 'no rows in ws13_chunks at all'),
        ('doc.no_phrase', 'chunks read, no phrase cleared the quality gate'),
        ('doc.only_known_triples', 'every phrase was already proposed before'),
        ('doc.no_distinctive_phrase', 'phrases probed, none distinctive'),
        ('doc.probe_budget', f'stopped at {MAX_PHRASE_PROBES} probes, '
                             f'candidates left'),
        ('doc.id_collision', 'slug already used'),
        ('doc.item_problems', 'rejected by the offline gate'),
        ('doc.accepted', 'proposed'),
    )),
    ('chunks', (
        ('chunk.scanned', f'read (at most {CHUNKS_PER_DOC} per document)'),
        ('chunk.no_phrase', 'yielded no eligible phrase'),
    )),
    ('phrases', (
        ('phrase.seen', 'non-blank line/sentence pieces considered'),
        ('phrase.too_short', f'under {MIN_QUOTE_CHARS} characters'),
        ('phrase.too_long', f'over {MAX_QUOTE_CHARS} characters'),
        ('phrase.too_few_words', f'under {MIN_QUOTE_WORDS} words'),
        ('phrase.dirty', f'clean_ratio under {MIN_CLEAN_RATIO}'),
        ('phrase.digit_run', 'a coordinate table, not a sentence'),
        ('phrase.no_long_word', 'no run of four letters'),
        ('phrase.mostly_nonalpha', 'under half letters'),
        ('phrase.implausible', f'word_shape under {MIN_WORD_SHAPE_RATIO} or '
                               f'under {MIN_WORD_TOKENS} word-shaped tokens'),
        ('phrase.nfkc_drift', 'NFKC rewrote it; the excerpt would not match'),
        ('phrase.past_excerpt_end', f'starts past character {MAX_PHRASE_END}'),
        ('phrase.straddles_excerpt_end', f'starts inside, ends past '
                                         f'{MAX_PHRASE_END}'),
        ('phrase.repeat_in_chunk', 'the same line again in one chunk'),
        ('phrase.eligible', 'eligible (distinct, per chunk)'),
        ('phrase.repeat_in_doc', 'the same line again in another chunk of '
                                 'the same document'),
    )),
    ('probes', (
        ('probe.known_triple', 'skipped: (sha256, page, quote) already used'),
        ('probe.no_match', 'phraseto_tsquery matched no document at all'),
        ('probe.too_common', 'matched more documents than --max-matches'),
        ('probe.self_miss', 'matched few documents, but not this page'),
        ('probe.distinctive', 'accepted'),
    )),
)


class Funnel:
    """A count per rejection reason, so one run says where candidates died.

    The measurement this exists for: a --balanced --strata-limit 400 run
    proposed 3 of 24, and nothing in the output distinguished "the strata
    query only returned nine originals" from "every phrase on every originals
    page is corpus-wide boilerplate". Those have opposite fixes and the tool
    cannot be run from a developer machine to find out -- it is SSM-only,
    in-VPC -- so the run itself has to say.

    Counts EVENTS, not distinct text. One line that appears on four pages is
    four phrase.seen, and pipelines/ws13_worker.chunk_pages() overlaps chunks
    by 400 characters, so a line in that overlap is counted once per chunk it
    lands in. Read the phrase counts as "work done", not "corpus content".
    Nothing here feeds a decision: every bump is off the control path, so the
    candidates chosen are identical with and without it.
    """

    def __init__(self):
        self.counts = collections.Counter()
        # Kept apart from counts because the label is a scope, not a reason:
        # under --balanced propose() runs once per class and once unscoped.
        self.strata = collections.Counter()

    def bump(self, reason, count=1):
        self.counts[reason] += count

    def strata_rows(self, scope, count):
        self.strata[scope] += count
        self.bump('doc.strata', count)

    def lines(self):
        """The funnel as printable lines, zeros included."""
        out = []
        for section, entries in FUNNEL_SECTIONS:
            out.append(f'{section}:')
            for key, label in entries:
                out.append(f'  {self.counts[key]:>7}  {key:<28} {label}')
        unknown = sorted(set(self.counts) - {key for _, entries
                                             in FUNNEL_SECTIONS
                                             for key, _ in entries})
        for key in unknown:
            # A reason added to the code and not to FUNNEL_SECTIONS still
            # prints. Silently dropping it would be the same class of bug this
            # whole counter exists to catch.
            out.append(f'  {self.counts[key]:>7}  {key:<28} (unlabelled)')
        return out

    def stalled_at(self):
        """The largest document-level rejection bucket, or None.

        The largest bucket, which is not the same claim as "the cause": a
        funnel is a chain, and relieving the widest stage only moves
        candidates to the next one.
        """
        # doc.already_taken is excluded for the same reason doc.rewalked is,
        # and leaving it in was an asymmetry rather than a decision: neither is
        # a filter. A document the fixture already covers, or that an earlier
        # pass of this run already accepted, failed nothing -- naming it as the
        # widest stage points the operator at a bucket with no fix. Measured
        # once at 'doc.already_taken (6)' on an end-to-end run.
        buckets = [(self.counts[key], key) for key, _ in FUNNEL_SECTIONS[0][1]
                   if key.startswith('doc.') and key not in (
                       'doc.strata', 'doc.examined', 'doc.accepted',
                       'doc.not_reached', 'doc.rewalked',
                       'doc.already_taken')]
        count, key = max(buckets) if buckets else (0, None)
        return (key, count) if count else None


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
    parser.add_argument('--per-stratum', type=int,
                        default=DEFAULT_PER_STRATUM,
                        help='documents to draw from each (class, state, '
                             'doc_type) partition; the knob that gives a '
                             'narrow class enough rows to fill a quota')
    parser.add_argument('--dry-run', action='store_true',
                        help='print the candidates and write nothing')
    args = parser.parse_args(argv)
    if not (args.balanced or args.admission_class or args.state
            or args.doc_type):
        parser.error('a selector is required: --balanced, --admission-class, '
                     '--state or --doc-type')
    if not args.dsn:
        parser.error('--dsn is required (or set WS13_DB_DSN)')
    if args.per_stratum < 1:
        # A depth of 0 returns no rows at all, and the funnel would then read
        # as an empty corpus rather than as a mistyped flag.
        parser.error('--per-stratum must be at least 1')
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


Shape = collections.namedtuple('Shape', 'ratio words junk skipped')


def _case_class(part):
    """'U', 'L', 'T' for an all-letter part, or None when the case is broken.

    None is the OCR tell: 'IDt' and 'fJC' are neither one case nor Titlecase,
    and neither is 'PbS' -- token_shape() is what decides that a SHORT
    case-broken part is a formula-or-fragment to abstain on rather than junk.
    'McKinley' is exempted because it is a place name across this whole
    corpus; 'MacDonald' too, and CAMEL_NAME_RE covers the 'DeLamar' family.
    No other exception is made, so 'iPhone'-shaped tokens read as broken.
    """
    stem = MC_PREFIX_RE.sub('', part) or part
    if stem.isupper():
        return 'U'
    if stem.islower():
        return 'L'
    if stem[0].isupper() and stem[1:].islower():
        return 'T'
    if CAMEL_NAME_RE.match(part):
        return 'T'
    return None


def fold_token(token):
    """`token` with its combining accents dropped, so shape rules can be ASCII.

    NFKD splits 'ñ' into 'n' and a combining tilde and the mark is discarded,
    so 'Cañon' and 'Brûlé' are judged as 'Canon' and 'Brule'. candidate_phrases
    applies NFKC, which does NOT decompose these, and clean_ratio scores them
    0.92-0.95, so without this fold they arrived at the shape rules intact and
    were called junk. A letter whose diacritic is a stroke rather than a
    combining mark -- 'ø', 'đ', 'ł' -- does not decompose and still reads as
    junk; each is one token and the ratio absorbs it. Text in, text out: no
    lookup, no state, so determinism is untouched.
    """
    return ''.join(char for char in unicodedata.normalize('NFKD', token)
                   if not unicodedata.combining(char))


def token_shape(token):
    """WORD, JUNK, SKIP, SINGLE or STRAY for one whitespace-delimited token.

    SKIP is an abstention, not a pass: a number, a date, a dollar figure, a
    reference like 'T12N' or 'SiO2', or a short case-broken token like 'PbS'
    has no word shape to judge, and counting it either way would make an assay
    row score like prose. Those tokens are left out of the ratio entirely.

    SINGLE and STRAY are both single letters, deferred to word_shape(), which
    judges a run of them by its company. The split is by case: an uppercase
    single letter is an initial or an agency abbreviation, a lowercase one
    outside SINGLE_LETTER_WORDS is the OCR of a vertical rule.
    """
    core = EDGE_PUNCT_RE.sub('', fold_token(token))
    if not core:
        return SKIP                      # a rule of dots, a bare bracket
    if any(char.isdigit() for char in core):
        return JUNK if LETTER_DIGIT_RE.search(core) else SKIP
    if len(core) == 1:
        return SINGLE if (core.isupper()
                          or core in SINGLE_LETTER_WORDS) else STRAY
    if not WORD_TOKEN_RE.match(core):
        return JUNK
    classes = set()
    for part in re.split(r"['-]", core):
        if (len(part) > 2 and not any(char in VOWELS for char in part)
                and not (len(part) <= ABBREV_MAX_LETTERS
                         and part[0].isupper())):
            # Over two letters, because 'ft', 'Mt' and 'St' are abbreviations
            # a mining file is full of, while 'rbc' and 'nnd' are misreads.
            # The capitalised exemption is ABBREV_MAX_LETTERS above.
            return JUNK
        if CONSONANT_RUN_RE.search(part):
            return JUNK
        case = _case_class(part)
        if case is None:
            return SKIP if len(part) <= MIXED_CASE_MAX else JUNK
        classes.add(case)
    if {'U', 'L'} <= classes:
        # An all-caps part joined to an all-lowercase one: "UNI'l'ED",
        # "S'l'A'l'ES" -- scanned small caps with an l read for an apostrophe
        # pair. Titlecase mixes freely with either, so "d'Alene" and "O'Brien"
        # are words. 'X-ray' is the casualty, and it is one token.
        return JUNK
    return WORD


def word_shape(phrase):
    """How much of a phrase looks like words: ratio, and the three tallies.

    Pure function of the text, which determinism requires: the same phrase
    scores the same on every run, on any host, with no corpus lookup.
    """
    shapes = [token_shape(token) for token in (phrase or '').split()]
    judged, run, stray = [], 0, False
    for shape in shapes + [None]:
        if shape in (SINGLE, STRAY):
            # A run of single letters is judged by its company, and the
            # company is judged by CASE, not by length. Length was the rule
            # and it was wrong in both directions: 'U. S. G. S.' and
            # 'U. S. B. M.' are runs of four, and a legal description closing
            # '... R. 21 E., W. M.' -- the Willamette and Boise meridians end
            # nearly every Oregon and Idaho description in this corpus -- is a
            # run of three, so an all-caps plat header scored 0.667 and was
            # rejected. Worse, the whole run was expanded to junk, so one
            # four-letter agency abbreviation weighed four times an entire
            # misread word. An uppercase run now costs nothing; one stray
            # lowercase letter condemns the run it sits in, because 'I i l l'
            # is a ruled form read as letters and 'U S G l S' is a misread run
            # rather than an abbreviation.
            run += 1
            stray = stray or shape == STRAY
            continue
        if run:
            judged.extend([JUNK if stray else SKIP] * run)
            run, stray = 0, False
        if shape is not None:
            judged.append(shape)
    words, junk = judged.count(WORD), judged.count(JUNK)
    total = words + junk
    # 0.0, not 1.0, when nothing was judgeable: a line of pure numbers has no
    # word shape and must not be scored as if it had a perfect one.
    return Shape(words / total if total else 0.0, words, junk,
                 judged.count(SKIP))


def word_shape_ratio(phrase):
    """The ratio alone, for a caller that wants only the number."""
    return word_shape(phrase).ratio


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


def candidate_phrases(text, funnel=None):
    """(phrase, offset) pairs from one chunk that could identify a page.

    Split on line breaks first and sentence ends second: OCR output keeps the
    page's line structure, and a line is what a reader can find again on the
    page when they verify the item.

    A piece has to be legible (clean_ratio) AND has to be language
    (word_shape): the two measure different things, and the second is what
    keeps misread OCR out of a fixture a human has to paraphrase by hand.

    The offset is the phrase's start in the WHITESPACE-NORMALISED chunk --
    the same text infra/ws13_query_lambda.excerpt() windows -- and anything
    reaching past MAX_PHRASE_END is dropped here rather than becoming an item
    the live gate can never pass. Best first, by specificity only; position is
    a hard filter, not a ranking, so a rerun still picks the same quote.

    `funnel` only counts. It is read by nothing here and the returned list is
    byte-identical whether one is passed or not, so instrumenting a run cannot
    change which candidates it proposes.
    """
    funnel = funnel or Funnel()
    flat = normalize(text)
    phrases = {}
    for raw_line in re.split(r'[\r\n]+', text or ''):
        for part in re.split(r'(?<=[.;:])\s+', raw_line):
            phrase = normalize(unicodedata.normalize('NFKC', part))
            if not phrase:
                # A blank piece is not a rejected candidate. Counting it as
                # one would report a page of double-spaced OCR as hundreds of
                # too-short phrases and bury the reason the page really failed.
                continue
            funnel.bump('phrase.seen')
            # One check, split in two for the count only: the two ends fail for
            # opposite reasons -- a heading fragment versus a paragraph run
            # together by the sentence split -- and the fix differs.
            if len(phrase) < MIN_QUOTE_CHARS:
                funnel.bump('phrase.too_short')
                continue
            if len(phrase) > MAX_QUOTE_CHARS:
                funnel.bump('phrase.too_long')
                continue
            words = phrase.split(' ')
            if len(words) < MIN_QUOTE_WORDS:
                funnel.bump('phrase.too_few_words')
                continue
            if clean_ratio(phrase) < MIN_CLEAN_RATIO:
                funnel.bump('phrase.dirty')
                continue
            if DIGIT_RUN_RE.search(phrase):
                funnel.bump('phrase.digit_run')
                continue        # a coordinate table, not a sentence
            if not re.search(r'[A-Za-z]{4,}', phrase):
                funnel.bump('phrase.no_long_word')
                continue
            if sum(char.isalpha() for char in phrase) < len(phrase) // 2:
                funnel.bump('phrase.mostly_nonalpha')
                continue
            shape = word_shape(phrase)
            if (shape.words < MIN_WORD_TOKENS
                    or shape.ratio < MIN_WORD_SHAPE_RATIO):
                # Last of the quality checks and the most expensive, so it runs
                # last. A line that gets here is legible; this is the one that
                # asks whether it is language. Expect it to cost yield on
                # scanned classes -- that is not a regression, it is the cost
                # of not proposing an item no human can complete.
                funnel.bump('phrase.implausible')
                continue
            start = flat.find(phrase)
            if start < 0:
                # NFKC rewrote the line (a ligature, a full-width digit). The
                # retrieval excerpt collapses whitespace and nothing else, so
                # the stored quote would not match what it returns.
                funnel.bump('phrase.nfkc_drift')
                continue
            if start + len(phrase) > MAX_PHRASE_END:
                # Two different problems, counted apart because they answer
                # different questions. past_excerpt_end is chunk tail material
                # -- text the live excerpt window cannot reach in the ordinary
                # case, and no phrase length would help. straddles_excerpt_end
                # is a phrase that begins inside the window and runs out of it,
                # which is a length interaction, not a position one.
                funnel.bump('phrase.past_excerpt_end' if start > MAX_PHRASE_END
                            else 'phrase.straddles_excerpt_end')
                continue
            if phrase in phrases:
                # The first offset wins, as before: a repeated line is nearest
                # the excerpt window at its first occurrence.
                funnel.bump('phrase.repeat_in_chunk')
            phrases.setdefault(phrase, start)
    funnel.bump('phrase.eligible', len(phrases))
    # Deterministic: the phrase text breaks ties so a rerun over an unchanged
    # chunk picks the same quote.
    return sorted(phrases.items(),
                  key=lambda item: (-specificity(item[0]), item[0]))


DISTINCTIVE = 'distinctive'


def distinctiveness(conn, phrase, sha256, page, max_matches):
    """Why a phrase is or is not usable: DISTINCTIVE, or the reason it failed.

    Same two queries in the same order as the boolean it replaced, and the
    second is still skipped when the first already decides, so a probe costs
    exactly what it always did. The three failures are separated because they
    are three unrelated problems:

      'too_common'  the phrase is boilerplate. Expected, and the whole point
                    of the filter -- a letterhead in forty mine files cannot
                    prove which one was retrieved.
      'no_match'    phraseto_tsquery matched nothing anywhere, including the
                    chunk the phrase was copied out of. That is a phrase whose
                    tokens all fall out of the 'english' configuration (all
                    stop words) or OCR debris that indexed as nothing.
      'self_miss'   few enough documents matched, but not this sha256/page.
                    This should be zero: the phrase is a verbatim substring of
                    that chunk's own text and the chunk's tsv is
                    to_tsvector('english', text) over it. A nonzero count means
                    tsv and text have drifted apart in the corpus, which is a
                    corpus bug, not a candidate that happened to be bad.
    """
    matches = conn.execute(DISTINCT_DOCS_SQL,
                           (phrase, max_matches + 1)).fetchone()[0]
    if matches == 0:
        return 'no_match'
    if matches > max_matches:
        return 'too_common'
    if not conn.execute(SELF_MATCH_SQL,
                        (sha256, page, phrase)).fetchone()[0]:
        return 'self_miss'
    return DISTINCTIVE


def is_distinctive(conn, phrase, sha256, page, max_matches):
    """True when the phrase names few enough documents AND names this one."""
    return distinctiveness(conn, phrase, sha256, page,
                           max_matches) == DISTINCTIVE


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
    params.extend([args.per_stratum, args.seed, args.strata_limit])
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


def document_options(conn, args, sha256, funnel):
    """Every probeable (page, ordinal, phrase, offset) for one document.

    Ordered round robin: the best phrase of every sampled chunk, then the
    second-best of every chunk, and so on. Ordering by chunk first -- which is
    what the nested loops this replaced did -- spent the whole probe budget on
    the first chunk of a text-dense document and left pages 2..12 fetched and
    never read, so the more text a document had the fewer of its pages were
    looked at. Interleaving makes the budget buy DIFFERENT PAGES, which is what
    the distinctiveness filter rewards: the eight probes it had then, all spent
    on one page's boilerplate, were eight draws from one distribution, and a
    portal's letterhead is on page 1 of every sibling document but not on
    page 6.

    Deterministic on both keys. Within a chunk, candidate_phrases() is already
    ordered by specificity with the phrase text breaking ties; across chunks
    the CHUNKS_SQL md5 order is fixed by the seed and made a TOTAL order by
    the separators and the page/ordinal tiebreak in that statement, without
    which two chunks of one document could hash alike and swap places between
    runs. (rank, index) is unique per entry, so the sort below is total too.
    """
    chunks = conn.execute(CHUNKS_SQL,
                          (sha256, args.seed, CHUNKS_PER_DOC)).fetchall()
    ranked, seen = [], set()
    for index, (page, ordinal, text) in enumerate(chunks):
        funnel.bump('chunk.scanned')
        options = candidate_phrases(text, funnel)
        if not options:
            funnel.bump('chunk.no_phrase')
        rank = 0
        for phrase, offset in options:
            if phrase in seen:
                # ws13_worker.chunk_pages() overlaps chunks by 400 characters,
                # so a line in the overlap is a real duplicate, not a second
                # candidate. Probing it twice would spend the budget on the
                # same corpus-wide answer.
                funnel.bump('phrase.repeat_in_doc')
                continue
            seen.add(phrase)
            # `rank` counts what this chunk CONTRIBUTES, and it is incremented
            # here rather than by enumerate() over `options` for a reason the
            # interleave lives or dies on. Ranked before the dedupe, a chunk
            # whose best line already appeared in an earlier chunk contributed
            # no rank-0 entry at all, so the first round-robin pass skipped
            # that chunk entirely -- and a running header repeated on every
            # page is precisely the common case, the one the dedupe above was
            # written for. Measured on a 12-chunk document with a header on
            # every page: page 1 drew two probes, page 12 drew none, and the
            # document's only distinctive line was the one on page 12.
            ranked.append((rank, index, page, ordinal, phrase, offset))
            rank += 1
    ranked.sort(key=lambda option: option[:2])
    return bool(chunks), [option[2:] for option in ranked]


def propose(conn, args, used_shas, used_triples, used_ids, wanted, funnel,
            seen_docs=None):
    """Candidate items, at most one per document, at most `wanted` total.

    `seen_docs`, when given, accumulates every sha256 this run has already
    examined, so a later pass does not re-walk one. See propose_balanced().
    """
    proposed = []
    rows = strata(conn, args)
    # stratum_rank <= --per-stratum. At the depth of 1 this used to run at, the
    # row count WAS the number of (class, state, doc_type) combinations rather
    # than a sample of the class, so a class holding few combinations offered a
    # handful of rows however many documents it held -- which --strata-limit
    # could not raise, being a ceiling that class never reached. Depth is what
    # raises it; see DEFAULT_PER_STRATUM, including what it does not promise.
    # This count, per scope, is what the next live run has to be read for: it
    # is the number that says whether a class was strata-bound at all.
    funnel.strata_rows('+'.join(args.admission_class) or 'unscoped', len(rows))
    for index, row in enumerate(rows):
        sha256, admission_class, state, doc_type, title, doc_date = row
        if len(proposed) >= wanted:
            # Rows the quota never reached are not rejections and must not be
            # counted as any: they are the difference between "the corpus had
            # nothing" and "we stopped asking".
            funnel.bump('doc.not_reached', len(rows) - index)
            break
        funnel.bump('doc.examined')
        if sha256 in used_shas:
            funnel.bump('doc.already_taken')
            continue
        if seen_docs is not None:
            if sha256 in seen_docs:
                funnel.bump('doc.rewalked')
                continue
            seen_docs.add(sha256)
        had_chunks, options = document_options(conn, args, sha256, funnel)
        if not had_chunks:
            # Not the same as "no phrase passed", and the distinction decides
            # what to do: an indexed document with zero chunk rows is an
            # extraction that failed silently, and belongs in a requeue, not
            # in a phrase-filter discussion.
            funnel.bump('doc.no_chunks')
            continue
        picked, probes, eligible, unprobed = None, 0, len(options), False
        for page, ordinal, phrase, offset in options:
            if (sha256, page, phrase) in used_triples:
                funnel.bump('probe.known_triple')
                continue
            if probes >= MAX_PHRASE_PROBES:
                # An option this document had and never spent a probe on.
                unprobed = True
                break
            probes += 1
            verdict = distinctiveness(conn, phrase, sha256, page,
                                      args.max_matches)
            funnel.bump(f'probe.{verdict}')
            if verdict != DISTINCTIVE:
                continue
            picked = (page, ordinal, phrase, offset)
            break
        if not picked:
            # Four dead ends that argue for four different changes, and only
            # the counts tell them apart. doc.probe_budget is the narrow
            # claim: material was left when the tool stopped asking, so
            # MAX_PHRASE_PROBES is what bounded this document. A document
            # whose last probe was also its last candidate is not budget bound
            # and lands in doc.no_distinctive_phrase, where it belongs.
            if unprobed:
                funnel.bump('doc.probe_budget')
            elif not eligible:
                funnel.bump('doc.no_phrase')
            elif not probes:
                funnel.bump('doc.only_known_triples')
            else:
                funnel.bump('doc.no_distinctive_phrase')
            continue
        page, ordinal, phrase, offset = picked
        identifier = slug(admission_class, state, sha256, page)
        if identifier in used_ids:
            funnel.bump('doc.id_collision')
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
                f'{args.seed!r}) from ws13_chunks seed-sampled chunk '
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
            funnel.bump('doc.item_problems')
            print(f'rejected {identifier}: {"; ".join(problems)}',
                  file=sys.stderr)
            continue
        funnel.bump('doc.accepted')
        used_ids.add(identifier)
        used_shas.add(sha256)
        used_triples.add((sha256, page, phrase))
        proposed.append(candidate)
    return proposed


def propose_balanced(conn, args, used_shas, used_triples, used_ids, wanted,
                     funnel):
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
    # Every scoped pass draws from the same partitions the unscoped top-up
    # will, so without this the top-up re-walks documents that already failed
    # -- and they fail again identically, every filter being a pure function of
    # the same text, at the same probe cost. Sharing the set turns that spend
    # into rows the scoped passes never reached, which is the only place the
    # top-up can find anything. It counts as doc.rewalked, not as a rejection.
    seen_docs = set()
    for name in ADMISSION_CLASSES:
        if quotas[name] <= 0:
            continue
        scoped = argparse.Namespace(**vars(args))
        scoped.admission_class = [name]
        proposed.extend(propose(conn, scoped, used_shas, used_triples,
                                used_ids, quotas[name], funnel, seen_docs))
    if len(proposed) < wanted:
        # A class that cannot fill its quota must not shrink the set; the
        # top-up runs unrestricted and skips everything already taken.
        proposed.extend(propose(conn, args, used_shas, used_triples, used_ids,
                                wanted - len(proposed), funnel, seen_docs))
    return proposed


def report(items, fixture, wanted, funnel):
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
    report_funnel(funnel)


def report_funnel(funnel):
    """Where the candidates that were not proposed died.

    Printed on every run, not only a short one. A run that fills its quota
    still says what it cost, and the costs are what the next change to the
    filters has to be argued from -- this tool cannot be run outside the VPC,
    so a number nobody printed is a number nobody has.
    """
    print('\nrejection funnel')
    print('  limits in force: quote '
          f'{MIN_QUOTE_CHARS}-{MAX_QUOTE_CHARS} chars, >= {MIN_QUOTE_WORDS} '
          f'words, clean_ratio >= {MIN_CLEAN_RATIO}, word_shape >= '
          f'{MIN_WORD_SHAPE_RATIO} over >= {MIN_WORD_TOKENS} word-shaped '
          f'tokens; phrase must end by '
          f'character {MAX_PHRASE_END} of the normalised chunk (chunks run to '
          f'{CHUNK_CHARS_HINT}); <= {CHUNKS_PER_DOC} chunks sampled and '
          f'<= {MAX_PHRASE_PROBES} probes per document, one per chunk before '
          'any chunk gets a second')
    if funnel.strata:
        # The line that separates "this class yielded nothing" from "this
        # class was barely sampled": each row is one (state, doc_type)
        # combination, so a small number here is a corpus shape, not a filter.
        print('  strata rows by scope: '
              + ', '.join(f'{scope} {count}'
                          for scope, count in sorted(funnel.strata.items())))
    for line in funnel.lines():
        print('  ' + line)
    stalled = funnel.stalled_at()
    if stalled:
        key, count = stalled
        print(f'  largest document-level rejection: {key} ({count}). That is '
              'the widest stage, not necessarily the cause; a funnel is a '
              'chain and the next stage may reject the same documents.')


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

    funnel = Funnel()
    conn = connect(args.dsn)
    try:
        if args.balanced and not args.admission_class:
            proposed = propose_balanced(conn, args, used_shas, used_triples,
                                        used_ids, wanted, funnel)
        else:
            proposed = propose(conn, args, used_shas, used_triples, used_ids,
                               wanted, funnel)
    finally:
        conn.close()

    report(proposed, fixture, wanted, funnel)
    if not proposed:
        # This used to name the distinctiveness filter, which was a guess: a
        # run proposing nothing because every stratum document was already in
        # the fixture, or held no chunk rows, printed the same sentence and
        # sent the operator to --max-matches, the one knob that must not be
        # widened. The funnel above says which stage it actually was.
        print('no candidate was proposed; the funnel above names the stage '
              'they died at. Do not widen --max-matches to buy yield: a '
              'phrase matching many documents cannot prove which one was '
              'retrieved, which is the only thing this fixture tests',
              file=sys.stderr)
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
