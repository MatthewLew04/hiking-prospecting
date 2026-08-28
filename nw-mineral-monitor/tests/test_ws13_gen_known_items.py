"""The candidate funnel in tools/ws13_gen_known_items.py, offline.

The tool it covers cannot be run from a developer machine: it reaches
ws13_documents and ws13_chunks over SSM from inside the VPC, read-only, and
there is no local corpus to point it at. So every filter here is exercised
over synthetic text, and the database is a stub that answers the four
statements the tool issues.

What these tests are for, specifically. A --balanced --strata-limit 400 run
proposed 3 of 24 candidates with 0 of 3 rights classes covered by
'originals', and the run printed nothing that said which stage rejected the
other 21. The funnel counters exist to answer that, and a counter nobody
checks is a counter that lies -- so the accounting invariant (every examined
document lands in exactly one outcome bucket) is asserted here rather than
trusted.
"""
import contextlib
import hashlib
import io
import re
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
sys.path.insert(0, str(TOOLS))

# A deployment dependency of the in-VPC host, not of the test host.
if "psycopg" not in sys.modules:
    try:
        import psycopg                                    # noqa: F401
    except ImportError:
        _stub = types.ModuleType("psycopg")
        _stub.connect = mock.MagicMock()
        sys.modules["psycopg"] = _stub

import ws13_gen_known_items as gen                        # noqa: E402

# Real-looking digests. item_problems() rejects a sha256 built from one or
# two repeated characters as synthesised, so "a" * 64 would exercise that
# branch instead of the one under test.
SHA = hashlib.sha256(b"lava creek adit").hexdigest()
OTHER_SHA = hashlib.sha256(b"bear gulch adit").hexdigest()
THIRD_SHA = hashlib.sha256(b"silver king shaft").hexdigest()

# A real proposed candidate, and the line the word-shape filter was built
# around. clean_ratio() scores it 0.919 against a floor of 0.90 and would still
# admit it -- the characters are individually legal and only their arrangement
# is impossible -- so what rejects it is word_shape(), at 0.500. (0.545 until
# the single-letter run rule became case-aware: the trailing '~l' is a stray
# lowercase letter, which is now junk instead of an abstention.)
OCR_GARBAGE = ("Ii'ille<l Out anu )Iailt•d to rbc ltfit:e of \"'tate "
               "IDt-:fJC:' ·to1· of ~l")
GOOD_LINE = ("The Lava Creek adit was driven 1,240 feet northwest along the "
             "vein and exposed a silver-lead shoot averaging fourteen ounces "
             "of silver per ton in 1938.")


def args(**overrides):
    """The subset of the parsed namespace propose() and strata() read."""
    values = dict(seed="ws13-known-items", admission_class=[], state=[],
                  doc_type=[], strata_limit=400, max_matches=3,
                  per_stratum=gen.DEFAULT_PER_STRATUM)
    values.update(overrides)
    return types.SimpleNamespace(**values)


class Result:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return list(self.rows)

    def fetchone(self):
        return self.rows[0] if self.rows else None


class FakeConn:
    """Answers the four statements the tool issues, and counts them.

    corpus_docs maps a phrase to how many documents contain it; anything not
    named is in exactly one, which is the distinctive case. The self-match
    answer is derived from the chunks actually handed out, so it agrees with
    the corpus by construction -- the same property the real ws13_chunks.tsv
    has, being to_tsvector('english', text) over that same text.
    """

    def __init__(self, docs, chunks, corpus_docs=None):
        self.docs = docs
        self.chunks = chunks
        self.corpus_docs = corpus_docs or {}
        self.probes = 0
        self.probed = []
        self.statements = []

    def execute(self, sql, params=()):
        self.statements.append((sql, tuple(params)))
        if "stratum_rank" in sql:
            return Result(self.docs)
        if "FROM ws13_chunks c" in sql:
            return Result(self.chunks.get(params[0], []))
        if "SELECT count(*)" in sql:
            self.probes += 1
            phrase, cap = params
            self.probed.append(phrase)
            return Result([(min(self.corpus_docs.get(phrase, 1), cap),)])
        if "SELECT EXISTS" in sql:
            sha256, page, phrase = params
            rows = self.chunks.get(sha256, [])
            return Result([(any(phrase in gen.normalize(text)
                                for chunk_page, _, text in rows
                                if chunk_page == page),)])
        raise AssertionError(f"unexpected statement: {sql[:60]}")

    def close(self):
        pass


def document(sha256=SHA, admission_class="originals", state="ID",
             doc_type="mine-file", title="Lava Creek mine file",
             doc_date="1938"):
    return (sha256, admission_class, state, doc_type, title, doc_date)


def propose(conn, wanted=1, funnel=None, **overrides):
    funnel = funnel or gen.Funnel()
    items = gen.propose(conn, args(**overrides), set(), set(), set(), wanted,
                        funnel)
    return items, funnel


class CandidatePhraseTests(unittest.TestCase):
    """The per-line quality gate, and the counter hung off it."""

    def phrases(self, text):
        funnel = gen.Funnel()
        return gen.candidate_phrases(text, funnel), funnel.counts

    def test_funnel_does_not_change_the_result(self):
        # The whole instrumentation claim in one assertion: counting is off
        # the control path, so an instrumented run proposes what an
        # uninstrumented one would have.
        text = "\n".join([GOOD_LINE, OCR_GARBAGE, "short", "x" * 400])
        self.assertEqual(gen.candidate_phrases(text),
                         gen.candidate_phrases(text, gen.Funnel()))

    def test_a_good_line_is_eligible_at_its_offset(self):
        phrases, counts = self.phrases(GOOD_LINE)
        self.assertEqual([phrase for phrase, _ in phrases], [GOOD_LINE])
        self.assertEqual(phrases[0][1], 0)
        self.assertEqual(counts["phrase.eligible"], 1)

    def test_blank_lines_are_not_counted_as_rejected_candidates(self):
        # A page of double-spaced OCR would otherwise report hundreds of
        # too-short phrases and bury the reason it really failed.
        _, counts = self.phrases("\n\n   \n\n" + GOOD_LINE + "\n\n\n")
        self.assertEqual(counts["phrase.seen"], 1)
        self.assertEqual(counts["phrase.too_short"], 0)

    def test_each_quality_reason_is_counted_apart(self):
        cases = {
            "phrase.too_short": "Ore was shipped.",
            "phrase.too_long": "The vein was traced " + "north " * 60,
            "phrase.too_few_words": "Antimony-tungsten-molybdenum-scheelite"
                                    "-wolframite mineralisation",
            "phrase.dirty": "░▒▓ §¶†‡ «»‹› —–— µ±≠ ∑∏√ ▓▒░ ‡†¶§ ‹›«» "
                            "≠±µ √∏∑ ░▒▓ §¶†‡ «»‹›",
            "phrase.digit_run": "The claim recorder logged entry "
                                "1234567890123 for the Lava Creek group",
            "phrase.mostly_nonalpha": "Assay 1 2 3 4 5 6 7 8 9 10 11 12 13 "
                                      "14 15 16 17 18 19 20 21 22 23",
        }
        for reason, line in cases.items():
            with self.subTest(reason=reason):
                phrases, counts = self.phrases(line)
                self.assertEqual(phrases, [])
                self.assertEqual(counts[reason], 1, dict(counts))

    def test_no_long_word_is_its_own_reason(self):
        line = "a bc de fg hi jk lm no pq rs tu vw xy za bc de fg hi jk lm"
        phrases, counts = self.phrases(line)
        self.assertEqual(phrases, [])
        self.assertEqual(counts["phrase.no_long_word"], 1)

    def test_position_rejections_separate_tail_text_from_a_straddle(self):
        # The question these two answer is different. A phrase that STARTS
        # past MAX_PHRASE_END is chunk tail material no length rule reaches; a
        # phrase that starts inside and ends outside is a length interaction.
        filler = "Sample assays are tabulated in the appendix. " * 20
        self.assertGreater(len(filler), gen.MAX_PHRASE_END)
        _, counts = self.phrases(filler + "\n" + GOOD_LINE)
        self.assertEqual(counts["phrase.past_excerpt_end"], 1)
        self.assertEqual(counts["phrase.straddles_excerpt_end"], 0)

        head = "x" * (gen.MAX_PHRASE_END - 40) + "\n"
        _, counts = self.phrases(head + GOOD_LINE)
        self.assertEqual(counts["phrase.straddles_excerpt_end"], 1)
        self.assertEqual(counts["phrase.past_excerpt_end"], 0)

    def test_a_repeated_line_is_counted_once_at_its_first_offset(self):
        phrases, counts = self.phrases(GOOD_LINE + "\n" + GOOD_LINE)
        self.assertEqual(len(phrases), 1)
        self.assertEqual(phrases[0][1], 0)
        self.assertEqual(counts["phrase.repeat_in_chunk"], 1)
        self.assertEqual(counts["phrase.eligible"], 1)
        self.assertEqual(counts["phrase.seen"], 2)

    def test_ocr_garbage_clears_the_character_gate_and_dies_on_word_shape(self):
        """The measured hole, and the measurement that closes it.

        clean_ratio() measures character legality, not word plausibility, and
        misread OCR is legal characters in an impossible arrangement. The
        0.919 is still pinned -- the character gate has NOT been tightened and
        would still admit this line on its own -- and the rejection now comes
        from word_shape, which scores the same line 0.500.

        Reverting the word-shape filter makes this test fail on the last two
        assertions, which is the point of keeping the first two.
        """
        self.assertAlmostEqual(gen.clean_ratio(OCR_GARBAGE), 0.919, places=3)
        self.assertGreaterEqual(gen.clean_ratio(OCR_GARBAGE),
                                gen.MIN_CLEAN_RATIO)
        phrases, counts = self.phrases(OCR_GARBAGE)
        self.assertEqual(phrases, [])
        self.assertEqual(counts["phrase.dirty"], 0)
        self.assertEqual(counts["phrase.implausible"], 1)


class DistinctivenessTests(unittest.TestCase):
    """Three unrelated failures, told apart."""

    def conn(self, corpus_docs=None):
        chunks = {SHA: [(4, 0, GOOD_LINE)]}
        return FakeConn([document()], chunks, corpus_docs)

    def test_a_phrase_in_one_document_on_its_own_page_is_distinctive(self):
        verdict = gen.distinctiveness(self.conn(), GOOD_LINE, SHA, 4, 3)
        self.assertEqual(verdict, gen.DISTINCTIVE)

    def test_boilerplate_is_too_common(self):
        conn = self.conn({GOOD_LINE: 40})
        self.assertEqual(gen.distinctiveness(conn, GOOD_LINE, SHA, 4, 3),
                         "too_common")

    def test_a_phrase_indexing_to_nothing_is_not_reported_as_common(self):
        conn = self.conn({GOOD_LINE: 0})
        self.assertEqual(gen.distinctiveness(conn, GOOD_LINE, SHA, 4, 3),
                         "no_match")

    def test_a_page_mismatch_is_its_own_verdict(self):
        # self_miss should never be seen in production: the phrase is a
        # verbatim substring of that chunk's own text. A nonzero count means
        # ws13_chunks.tsv has drifted from ws13_chunks.text.
        conn = self.conn()
        self.assertEqual(gen.distinctiveness(conn, GOOD_LINE, SHA, 9, 3),
                         "self_miss")

    def test_the_boolean_wrapper_still_agrees(self):
        conn = self.conn({GOOD_LINE: 40})
        self.assertFalse(gen.is_distinctive(conn, GOOD_LINE, SHA, 4, 3))
        self.assertTrue(gen.is_distinctive(self.conn(), GOOD_LINE, SHA, 4, 3))

    def test_a_decided_probe_costs_one_statement(self):
        # The count is capped at max_matches + 1 and the self-match query is
        # skipped once the first answer decides, so instrumenting the probe
        # did not make it more expensive.
        conn = self.conn({GOOD_LINE: 40})
        gen.distinctiveness(conn, GOOD_LINE, SHA, 4, 3)
        self.assertEqual(conn.probes, 1)


class FunnelAccountingTests(unittest.TestCase):
    """Every examined document lands in exactly one outcome bucket."""

    OUTCOMES = ("doc.already_taken", "doc.rewalked", "doc.no_chunks",
                "doc.no_phrase", "doc.only_known_triples",
                "doc.no_distinctive_phrase", "doc.probe_budget",
                "doc.id_collision", "doc.item_problems", "doc.accepted")

    def assert_balanced(self, funnel):
        counts = funnel.counts
        self.assertEqual(counts["doc.strata"],
                         counts["doc.examined"] + counts["doc.not_reached"])
        self.assertEqual(counts["doc.examined"],
                         sum(counts[key] for key in self.OUTCOMES))

    def test_a_document_with_no_chunks_is_not_a_phrase_failure(self):
        # The distinction decides what to do: an indexed document with no
        # chunk rows is a silently failed extraction and belongs in a
        # requeue, not in an argument about the phrase filters.
        conn = FakeConn([document()], {})
        items, funnel = propose(conn)
        self.assertEqual(items, [])
        self.assertEqual(funnel.counts["doc.no_chunks"], 1)
        self.assertEqual(funnel.counts["doc.no_phrase"], 0)
        self.assertEqual(funnel.counts["chunk.scanned"], 0)
        self.assert_balanced(funnel)

    def test_chunks_without_a_usable_line_are_a_phrase_failure(self):
        conn = FakeConn([document()], {SHA: [(1, 0, "TABLE 3\n\n1 2 3\n")]})
        items, funnel = propose(conn)
        self.assertEqual(items, [])
        self.assertEqual(funnel.counts["doc.no_phrase"], 1)
        self.assertEqual(funnel.counts["doc.no_chunks"], 0)
        self.assertEqual(funnel.counts["chunk.scanned"], 1)
        self.assertEqual(funnel.counts["chunk.no_phrase"], 1)
        self.assert_balanced(funnel)

    def test_boilerplate_pages_exhaust_the_probe_budget(self):
        # The measured shape of a front matter page: every line is legible,
        # every line is in hundreds of documents. More candidates than probes,
        # and the document is abandoned with candidates left unprobed -- which
        # is what separates doc.probe_budget from "the corpus had nothing to
        # offer".
        # Two lines per page over seven pages, so the whole pool cannot fit in
        # the budget however the probes are spread; each page stays well inside
        # MAX_PHRASE_END, which keeps this a probe-budget test rather than a
        # position test.
        lines = [f"Bulletin {number} of the United States Geological Survey"
                 for number in range(900, 914)]
        chunks = [(page, 0, "\n".join(lines[index:index + 2]))
                  for page, index in enumerate(range(0, len(lines), 2), 1)]
        conn = FakeConn([document()], {SHA: chunks},
                        {line: 40 for line in lines})
        items, funnel = propose(conn)
        self.assertEqual(items, [])
        self.assertEqual(funnel.counts["phrase.eligible"], len(lines))
        self.assertEqual(funnel.counts["probe.too_common"],
                         gen.MAX_PHRASE_PROBES)
        self.assertEqual(funnel.counts["doc.probe_budget"], 1)
        self.assertEqual(funnel.counts["doc.no_distinctive_phrase"], 0)
        self.assert_balanced(funnel)

    def test_probing_out_the_pool_is_not_the_budget(self):
        line = "Bulletin 927 of the United States Geological Survey"
        conn = FakeConn([document()], {SHA: [(1, 0, line)]}, {line: 40})
        items, funnel = propose(conn)
        self.assertEqual(items, [])
        self.assertEqual(funnel.counts["probe.too_common"], 1)
        self.assertEqual(funnel.counts["doc.no_distinctive_phrase"], 1)
        self.assertEqual(funnel.counts["doc.probe_budget"], 0)
        self.assert_balanced(funnel)

    def test_a_document_whose_last_candidate_was_its_last_probe(self):
        # Exactly MAX_PHRASE_PROBES candidates, all probed, all boilerplate.
        # probes == MAX_PHRASE_PROBES here, but nothing was left unasked, so
        # this document is not budget bound and raising the budget would not
        # rescue it. Counting it as doc.probe_budget would argue for exactly
        # the wrong change.
        lines = [f"Bulletin {number} of the United States Geological Survey"
                 for number in range(900, 900 + gen.MAX_PHRASE_PROBES)]
        conn = FakeConn([document()], {SHA: [(1, 0, "\n".join(lines))]},
                        {line: 40 for line in lines})
        items, funnel = propose(conn)
        self.assertEqual(items, [])
        self.assertEqual(funnel.counts["probe.too_common"],
                         gen.MAX_PHRASE_PROBES)
        self.assertEqual(funnel.counts["doc.probe_budget"], 0)
        self.assertEqual(funnel.counts["doc.no_distinctive_phrase"], 1)
        self.assert_balanced(funnel)

    def test_a_boilerplate_first_page_no_longer_hides_a_good_second_page(self):
        # The failure this replaces: probes were spent best-first WITHIN a
        # chunk, so a page of front matter ate the whole budget and page 2 --
        # fetched in the same query, carrying a perfectly distinctive line --
        # was never read. The more text a document had, the fewer of its pages
        # were looked at.
        #
        # Probes are now interleaved across chunks, so page 2's best line is
        # the second probe of the document, not the thirteenth. Revert the
        # interleave and this test fails with an empty proposal.
        lines = [f"Bulletin {number} of the United States Geological Survey"
                 for number in range(900, 900 + gen.MAX_PHRASE_PROBES)]
        conn = FakeConn(
            [document()],
            {SHA: [(1, 0, "\n".join(lines)), (2, 0, GOOD_LINE)]},
            {line: 40 for line in lines})
        items, funnel = propose(conn)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["quote"], GOOD_LINE)
        self.assertEqual(items[0]["page"], 2)
        self.assertEqual(funnel.counts["chunk.scanned"], 2)
        self.assertEqual(conn.probes, 2)
        self.assert_balanced(funnel)

    def test_material_left_unprobed_in_any_chunk_is_the_budget(self):
        # The other side of the same change: with every chunk's best line
        # probed and the budget gone, whatever is left anywhere in the
        # document is still material the tool stopped asking about.
        lines = [f"Bulletin {number} of the United States Geological Survey"
                 for number in range(900, 900 + gen.MAX_PHRASE_PROBES + 1)]
        chunks = [(page, 0, line) for page, line in enumerate(lines, 1)]
        conn = FakeConn([document()], {SHA: chunks},
                        {line: 40 for line in lines})
        items, funnel = propose(conn)
        self.assertEqual(items, [])
        self.assertEqual(funnel.counts["chunk.scanned"], len(lines))
        self.assertEqual(conn.probes, gen.MAX_PHRASE_PROBES)
        self.assertEqual(funnel.counts["doc.probe_budget"], 1)
        self.assert_balanced(funnel)

    def test_a_phrase_already_proposed_is_not_probed_again(self):
        conn = FakeConn([document()], {SHA: [(4, 0, GOOD_LINE)]})
        funnel = gen.Funnel()
        items = gen.propose(conn, args(), set(), {(SHA, 4, GOOD_LINE)}, set(),
                            1, funnel)
        self.assertEqual(items, [])
        self.assertEqual(conn.probes, 0)
        self.assertEqual(funnel.counts["probe.known_triple"], 1)
        self.assertEqual(funnel.counts["doc.only_known_triples"], 1)
        self.assert_balanced(funnel)

    def test_a_document_already_in_the_fixture_is_skipped_early(self):
        conn = FakeConn([document()], {SHA: [(4, 0, GOOD_LINE)]})
        funnel = gen.Funnel()
        items = gen.propose(conn, args(), {SHA}, set(), set(), 1, funnel)
        self.assertEqual(items, [])
        self.assertEqual(funnel.counts["doc.already_taken"], 1)
        self.assertEqual(funnel.counts["chunk.scanned"], 0)
        self.assert_balanced(funnel)

    def test_a_candidate_the_offline_gate_rejects_is_counted_not_written(self):
        # sha256 is not 64 hex characters, so item_problems() refuses it. The
        # tool must never hand the fixture something CI would reject.
        conn = FakeConn([document(sha256="not-a-sha")],
                        {"not-a-sha": [(4, 0, GOOD_LINE)]})
        with contextlib.redirect_stderr(io.StringIO()) as noise:
            items, funnel = propose(conn)
        self.assertIn("not 64 lowercase hex", noise.getvalue())
        self.assertEqual(items, [])
        self.assertEqual(funnel.counts["doc.item_problems"], 1)
        self.assertEqual(funnel.counts["doc.accepted"], 0)
        self.assert_balanced(funnel)

    def test_rows_the_quota_never_reached_are_not_rejections(self):
        docs = [document(), document(sha256=OTHER_SHA),
                document(sha256=THIRD_SHA)]
        chunks = {sha: [(4, 0, GOOD_LINE)] for sha, *_ in docs}
        conn = FakeConn(docs, chunks)
        items, funnel = propose(conn, wanted=1)
        self.assertEqual(len(items), 1)
        self.assertEqual(funnel.counts["doc.strata"], 3)
        self.assertEqual(funnel.counts["doc.examined"], 1)
        self.assertEqual(funnel.counts["doc.not_reached"], 2)
        self.assertEqual(funnel.counts["doc.accepted"], 1)
        self.assert_balanced(funnel)

    def test_a_proposed_item_records_the_probe_that_accepted_it(self):
        conn = FakeConn([document()], {SHA: [(4, 0, GOOD_LINE)]})
        items, funnel = propose(conn)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["quote"], GOOD_LINE)
        self.assertEqual(items[0]["page"], 4)
        self.assertFalse(items[0]["verified"])
        self.assertEqual(funnel.counts["probe.distinctive"], 1)
        self.assertEqual(funnel.counts["doc.accepted"], 1)
        self.assert_balanced(funnel)

    def test_a_rerun_over_the_same_corpus_proposes_the_same_item(self):
        docs = [document(), document(sha256=OTHER_SHA)]
        chunks = {SHA: [(4, 0, GOOD_LINE + "\n" + GOOD_LINE.replace(
            "1,240", "1,241"))],
            OTHER_SHA: [(2, 0, GOOD_LINE.replace("Lava", "Bear"))]}
        first, _ = propose(FakeConn(docs, chunks))
        second, _ = propose(FakeConn(docs, chunks))
        self.assertEqual(first, second)

    def test_strata_rows_are_labelled_by_scope(self):
        # The line that separates "this class yielded nothing" from "this
        # class was barely sampled at all", and the number the originals
        # diagnosis is still waiting on: nothing has ever printed a partition
        # count per class. At the stratum_rank = 1 this used to run at the row
        # count WAS that count; at --per-stratum it is min(--strata-limit,
        # partitions x depth), so the two together are what say whether a
        # class was strata-bound.
        conn = FakeConn([document()], {SHA: [(4, 0, GOOD_LINE)]})
        _, funnel = propose(conn, admission_class=["originals"])
        self.assertEqual(dict(funnel.strata), {"originals": 1})


class FunnelReportTests(unittest.TestCase):
    def test_every_reason_the_code_can_emit_has_a_printed_label(self):
        # A reason added to the code and not to FUNNEL_SECTIONS would still
        # print, as '(unlabelled)', but the label is the half a reader acts
        # on. This keeps the two in step.
        source = (Path(gen.__file__).read_text(encoding="utf-8"))
        emitted = set(re.findall(r"bump\(\s*'((?:doc|chunk|phrase|probe)\."
                                 r"[a-z_]+)'", source))
        emitted |= {f"probe.{name}" for name in
                    ("distinctive", "too_common", "no_match", "self_miss")}
        labelled = {key for _, entries in gen.FUNNEL_SECTIONS
                    for key, _ in entries}
        self.assertEqual(emitted - labelled, set())

    def test_zero_counts_are_printed(self):
        # A missing line and a zero read the same to an operator, and the zero
        # is the more useful of the two: 'phrase.past_excerpt_end 0' is what
        # rules MAX_PHRASE_END out as the binding constraint.
        text = "\n".join(gen.Funnel().lines())
        self.assertIn("phrase.past_excerpt_end", text)
        self.assertIn("doc.no_chunks", text)

    def test_the_widest_stage_is_reported_as_the_widest_stage(self):
        funnel = gen.Funnel()
        funnel.bump("doc.no_distinctive_phrase", 12)
        funnel.bump("doc.no_chunks", 3)
        self.assertEqual(funnel.stalled_at(),
                         ("doc.no_distinctive_phrase", 12))
        self.assertIsNone(gen.Funnel().stalled_at())


class WordShapeCalibrationTests(unittest.TestCase):
    """The calibration table: real mining-document lines against real garbage.

    Word shape is the filter that keeps a quote a human can actually verify.
    The measurement it has to make is not "is this legible" -- clean_ratio()
    answers that, and answered it 0.919 for the garbage below -- but "is this
    language", because step 3 of the review workflow is to paraphrase the
    question away from the quote's own words, and nobody can paraphrase
    "Ii'ille<l Out anu )Iailt*d". An item built on garbage cannot be completed
    at all; it burns one of 25 slots and a review cycle.

    Every ratio here is pinned to the value measured when the filter was
    written. Moving one of them is a deliberate act, not a side effect.
    """

    # Real lines, and what the shape measure scores them. The last two are the
    # ones the filter is allowed to be unhappy about and must still accept:
    # 'lengths' is a five-consonant run (NGTHS), and 'pct' and 'lbs' are
    # lowercase vowel-less abbreviations no rule without a dictionary tells
    # from 'rbc'. Neither should cost a page its only candidate, and the second
    # is the table's lower endpoint -- the number MIN_WORD_SHAPE_RATIO is set
    # against.
    REAL = (
        ("map legend, all caps",
         "UNITED STATES NUMBERED HIGHWAYS SHOWN IN RED", 1.000),
        ("formal report prose",
         "The ore bodies of the district occur as replacement deposits in the "
         "Mississippian limestone.", 1.000),
        ("table caption",
         "Table 4.-Assays of channel samples from the Bear Gulch tunnel, "
         "Custer County, Idaho", 1.000),
        ("assay figure",
         "The channel sample assayed 0.42 ounce of gold and 14.6 ounces of "
         "silver per ton", 1.000),
        ("proper noun and date",
         "On September 14, 1938, the Bureau of Mines examined the McKinley "
         "shaft", 1.000),
        ("legal description, single-letter abbreviations",
         "The claim corner lies 640 feet north of the section 14 corner in "
         "T 12 N, R 4 E", 1.000),
        ("apostrophe names",
         "The Coeur d'Alene district and the O'Brien tunnel were examined in "
         "1904", 1.000),
        ("prose with a figure and a compound", GOOD_LINE, 1.000),
        # The corpus's own vocabulary, every line of it a measured rejection
        # before the rule beside it was fixed. These are not edge cases: the
        # agencies, the meridians, the toponyms and the mineral formulae are
        # what a NW mineral file is made of, and a filter that scores them
        # below the floor is refusing the documents it exists to sample.
        ("agency initials, a run of four",
         "Published by the U S G S in cooperation with the Idaho Bureau of "
         "Mines and Geology", 1.000),
        ("plat header ending on a meridian",
         "MINERAL SURVEY 2148 SEC 22 T 8 S R 21 E W M BAKER COUNTY OREGON",
         1.000),
        ("aliquot parts and the Boise meridian",
         "The tract described as the S E 1/4 of the N W 1/4 of sec 6 T 3 N "
         "R 2 W B M", 1.000),
        ("Spanish toponyms",
         "The Cañon, Peña Blanca and Cañada grants adjoin the "
         "Río Salado placer ground", 1.000),
        ("French names and the accented Coeur d'Alène",
         "Located by Léon Brûlé and René Frère near "
         "the Coeur d'Alène river in 1884", 1.000),
        ("run-together Titlecase surnames",
         "The LaGrange, DeWitt, DeLamar and VanZandt lode claims of the Flint "
         "district", 1.000),
        ("mineral formulae, three in one line",
         "Galena (PbS), sphalerite (ZnS) and cerussite (PbCO3) occur with CaO "
         "rich gangue", 1.000),
        ("compass points and a vowel-less abbreviation",
         "The NNW trending WSW dipping quartz-adularia veins of the Florida "
         "Mtn area", 1.000),
        ("consonant-run casualty",
         "The lengths of the drill holes and the mineral rights of the "
         "eighty-acre tract", 0.929),
        # The floor's lower endpoint, and a cost the filter does not fix.
        # 'pct' and 'lbs' are three lowercase consonants, and so are 'rbc' and
        # 'nnd'; no rule without a dictionary separates them, so these are
        # counted as junk and the ratio is what carries the line.
        ("lowercase unit abbreviations, the worst real line",
         "Cyanidation of the DeLamar ore at 62 pct extraction with 3.2 lbs "
         "KCN per ton", 0.846),
    )

    # Garbage, one line per failure mode the filter is built to catch.
    GARBAGE = (
        ("the measured candidate", OCR_GARBAGE, 0.500),
        ("scanned small caps, l read as an apostrophe pair",
         "TIIE UNI'l'ED S'l'A'l'ES GEOLOGICAL SURVI~Y BULLE'l'IN", 0.333),
        ("1 read for l inside words",
         "Nan1e of clain1ant and Datc of locat1on of the m1ning cla1m", 0.545),
        ("a ruled form read as single letters",
         "The l l I i l form was filled out and mailed to the office", 0.667),
    )

    def test_every_real_line_scores_at_or_above_the_floor(self):
        for label, line, expected in self.REAL:
            with self.subTest(label=label):
                shape = gen.word_shape(line)
                self.assertAlmostEqual(shape.ratio, expected, places=3)
                self.assertGreaterEqual(shape.ratio, gen.MIN_WORD_SHAPE_RATIO)
                self.assertGreaterEqual(shape.words, gen.MIN_WORD_TOKENS)

    def test_every_garbage_line_scores_below_the_floor(self):
        for label, line, expected in self.GARBAGE:
            with self.subTest(label=label):
                shape = gen.word_shape(line)
                self.assertAlmostEqual(shape.ratio, expected, places=3)
                self.assertLess(shape.ratio, gen.MIN_WORD_SHAPE_RATIO)

    def test_the_floor_sits_in_the_gap_and_not_at_its_edge(self):
        # 0.846 real against 0.667 garbage, so the floor at 0.75 has 0.096 of
        # room on each side. A floor chosen at either edge of that gap is a
        # floor one new document moves; this records how much room it actually
        # has, so a later change to the rules has to say what it did to the
        # margin. The module comment quotes these two numbers, and it quoted
        # two that were in no table at all until this test was made to compute
        # them rather than to trust them.
        worst_real = min(gen.word_shape(line).ratio
                         for _, line, _ in self.REAL)
        best_garbage = max(gen.word_shape(line).ratio
                           for _, line, _ in self.GARBAGE)
        self.assertGreater(worst_real, gen.MIN_WORD_SHAPE_RATIO)
        self.assertLess(best_garbage, gen.MIN_WORD_SHAPE_RATIO)
        self.assertGreater(worst_real - best_garbage, 0.15)
        # Pinned, so a rule change that eats the margin has to say so here.
        self.assertAlmostEqual(worst_real, 0.846, places=3)
        self.assertAlmostEqual(best_garbage, 0.667, places=3)

    def test_the_real_lines_survive_the_whole_quality_gate(self):
        # The ratio is only half the claim: these have to come out of
        # candidate_phrases() as candidates, unsplit and unrejected by any of
        # the other checks. The map legend is the one that matters most -- an
        # all-caps line with no verb, a real accepted candidate, and exactly
        # the shape an over-aggressive plausibility rule would take.
        for label, line, _ in self.REAL:
            with self.subTest(label=label):
                self.assertEqual([phrase for phrase, _
                                  in gen.candidate_phrases(line)], [line])

    def test_the_garbage_lines_die_on_word_shape_and_not_by_accident(self):
        # Named explicitly: each of these clears the character gate and is
        # rejected by the shape gate. A garbage line that happened to be
        # rejected for length would prove nothing about this filter.
        for label, line, _ in self.GARBAGE:
            with self.subTest(label=label):
                funnel = gen.Funnel()
                self.assertGreaterEqual(gen.clean_ratio(line),
                                        gen.MIN_CLEAN_RATIO)
                self.assertEqual(gen.candidate_phrases(line, funnel), [])
                self.assertEqual(funnel.counts["phrase.implausible"], 1)
                self.assertEqual(funnel.counts["phrase.dirty"], 0)
                self.assertEqual(funnel.counts["phrase.too_short"], 0)

    def test_substitution_ocr_is_a_stated_limit_not_a_catch(self):
        """The documented hole, pinned so nobody discovers it in production.

        Letter-substitution OCR produces shape-plausible non-words -- 'tbe',
        'tlic', 'rcport' all have vowels, ordinary consonant runs and
        consistent case -- and only a lexicon separates them from real words.
        A lexicon would reject the mineral names, toponyms and Spanish and
        French place names this corpus is largely made of, so this filter does
        not attempt it and the human review step stays the backstop.
        """
        line = ("tbe Sccrctary of tlic Intcrior, Wasbington, D. C., rcport "
                "ou minc")
        self.assertAlmostEqual(gen.word_shape(line).ratio, 0.889, places=3)
        self.assertGreaterEqual(gen.word_shape(line).ratio,
                                gen.MIN_WORD_SHAPE_RATIO)


class WordShapeRuleTests(unittest.TestCase):
    """Each shape rule on its own, with the real word it is allowed to cost."""

    def test_a_token_is_pure_text_so_a_rerun_scores_it_the_same(self):
        # Determinism is a promise in the module docstring: no corpus lookup,
        # no clock, no randomness anywhere in the measure.
        for _ in range(3):
            self.assertEqual(gen.word_shape(OCR_GARBAGE),
                             gen.word_shape(OCR_GARBAGE))
        self.assertEqual(gen.word_shape_ratio(GOOD_LINE),
                         gen.word_shape(GOOD_LINE).ratio)

    def test_a_word_needs_a_vowel_once_it_is_over_two_letters(self):
        self.assertEqual(gen.token_shape("rbc"), gen.JUNK)
        self.assertEqual(gen.token_shape("nnd"), gen.JUNK)
        # Two letters are an abbreviation, and a mine file is full of them.
        for token in ("ft.", "Mt.", "St.", "yd"):
            with self.subTest(token=token):
                self.assertEqual(gen.token_shape(token), gen.WORD)

    def test_a_short_capitalised_token_may_have_no_vowel_at_all(self):
        # The rule as first written took the corpus's own shorthand: place
        # abbreviations, the compass points that describe every vein
        # orientation, and the mill reagents. Four of them in one line was
        # enough to put a real page under the floor.
        for token in ("Twp.", "Mtn.", "Mts.", "Crk.", "Spgs.", "Bldg.",
                      "NNW", "WNW", "SSW", "WSW", "KCN"):
            with self.subTest(token=token):
                self.assertNotEqual(gen.token_shape(token), gen.JUNK)

    def test_a_lowercase_vowel_less_token_is_junk_and_that_is_the_cost(self):
        # The stated limit, pinned so it is not rediscovered as a surprise.
        # 'pct' and 'lbs' are real and 'rbc' and 'nnd' are misreads, and all
        # four are three lowercase consonants. Shape cannot separate them and
        # a lexicon is the thing this measure refuses to have, so the units
        # are counted as junk and the ratio is what has to carry the line.
        for token in ("pct", "lbs", "cwt", "hrs"):
            with self.subTest(token=token):
                self.assertEqual(gen.token_shape(token), gen.JUNK)

    def test_an_accented_toponym_is_judged_on_its_folded_letters(self):
        # The classes are ASCII, so without the diacritic fold every one of
        # these was junk -- and 'Cañon', 'Cañada', 'Peña' and 'Río' are
        # pervasive in western US mining files, with 'Coeur d\'Alène' the older
        # Idaho spelling. Four in one line scored 0.692 and was rejected.
        for token in ("Cañon", "Peña", "Cañada", "Río", "Brûlé", "Vallée",
                      "Frère", "d'Alène"):
            with self.subTest(token=token):
                self.assertEqual(gen.token_shape(token), gen.WORD)
        # The fold handles combining marks, not strokes. Stated because it is
        # a limit, not because it matters often: one token, absorbed.
        self.assertEqual(gen.token_shape("Sørensen"), gen.JUNK)

    def test_the_consonant_run_is_five_because_four_costs_mineral_rights(self):
        # The rule that would have been wrong. 'rights' and 'heights' carry a
        # four-consonant run (GHTS) and 'mineral rights' is core vocabulary in
        # this corpus, so the threshold is five.
        for token in ("rights", "heights", "Wright", "Schmidt"):
            with self.subTest(token=token):
                self.assertEqual(gen.token_shape(token), gen.WORD)
        self.assertEqual(gen.token_shape("Sccrctary"), gen.JUNK)

    def test_a_digit_inside_a_word_is_junk_and_at_an_edge_is_skipped(self):
        for token in ("Nan1e", "clain1ant", "locat1on"):
            with self.subTest(token=token):
                self.assertEqual(gen.token_shape(token), gen.JUNK)
        # References are written this way in every mine file; they carry no
        # word shape to judge, so they are left out of the ratio rather than
        # counted either way.
        for token in ("T12N", "No.3", "SiO2", "1,240", "$14.20", "400-foot"):
            with self.subTest(token=token):
                self.assertEqual(gen.token_shape(token), gen.SKIP)

    def test_broken_interior_case_is_junk_only_once_it_is_long(self):
        # 'fJC' starts lowercase and dies on the vowel rule before case is
        # reached; a long case-broken token is not a formula and stays junk.
        self.assertEqual(gen.token_shape("fJC"), gen.JUNK)
        self.assertEqual(gen.token_shape("WasbingtonDC"), gen.JUNK)
        for token in ("McKinley", "MacDonald", "O'Brien", "d'Alene",
                      "silver-lead", "UNITED", "adit", "Bayhorse"):
            with self.subTest(token=token):
                self.assertEqual(gen.token_shape(token), gen.WORD)

    def test_a_short_case_broken_token_is_abstained_on_not_condemned(self):
        # 'PbS' and 'IDt' have the same shape and no rule without an element
        # table tells a formula from a scanner fragment, so the measure
        # abstains rather than guessing -- which is what it already did one
        # character along, 'SiO2' being SKIP for carrying a digit. Counting
        # 'PbS' as junk cost a mineralogy line carrying three formulae its
        # candidate at 0.727.
        for token in ("PbS", "ZnS", "CaO", "HCl", "NaCN", "IDt"):
            with self.subTest(token=token):
                self.assertEqual(gen.token_shape(token), gen.SKIP)

    def test_run_together_titlecase_fragments_are_a_name(self):
        # DeLamar is an Owyhee County silver mine, so this is a place name in
        # the corpus's own region and not an artifact. Each capital needs a
        # lowercase RUN after it, which is what keeps 'IDt' and 'PbS' out.
        for token in ("DeLamar", "DeWitt", "LaGrange", "VanZandt"):
            with self.subTest(token=token):
                self.assertEqual(gen.token_shape(token), gen.WORD)

    def test_an_upper_part_joined_to_a_lower_part_is_scanned_small_caps(self):
        # "UNI'l'ED" is UNITED with the small-capital T read as l between two
        # apostrophes. Titlecase mixes freely with either case, which is what
        # keeps "d'Alene" a word.
        self.assertEqual(gen.token_shape("UNI'l'ED"), gen.JUNK)
        self.assertEqual(gen.token_shape("BULLE'l'IN"), gen.JUNK)
        self.assertEqual(gen.token_shape("d'Alene"), gen.WORD)

    def test_single_letters_are_judged_by_the_case_of_their_company(self):
        # Length was the rule and it was wrong in both directions. 'U. S. G. S'
        # and 'U. S. B. M.' are runs of four, and a legal description closing
        # 'R. 21 E., W. M.' is a run of three -- the Willamette and Boise
        # meridians end nearly every Oregon and Idaho description in this
        # corpus -- so a run of three condemned the corpus's own notation. And
        # because the whole run was expanded to junk, one four-letter agency
        # abbreviation weighed four times an entire misread word.
        self.assertEqual(gen.token_shape("T"), gen.SINGLE)
        self.assertEqual(gen.token_shape("l"), gen.STRAY)
        self.assertEqual(gen.word_shape("Corner of T 12 N and R 4 E").junk, 0)
        self.assertEqual(gen.word_shape(
            "Mapped by the U S G S and the U S B M in the same season").junk,
            0)
        self.assertEqual(gen.word_shape(
            "The claim lies in sec 6 T 3 N R 2 W B M of Baker County").junk, 0)
        # A stray lowercase letter condemns the run it sits in: 'I i l l' is a
        # ruled form read as letters, and 'U S G l S' is a misread run rather
        # than an abbreviation. The article and the 'x' of "a 6 x 8 raise" are
        # the two lowercase exceptions.
        self.assertEqual(gen.word_shape("The form I i l l was filed").junk, 4)
        self.assertEqual(gen.word_shape("Driven as a 6 x 8 raise above the "
                                        "level").junk, 0)

    def test_punctuation_inside_a_token_is_a_scanner_artifact(self):
        for token in ("ltfit:e", "Ii'ille<l", "locat.ion", "IDt-:fJC:'"):
            with self.subTest(token=token):
                self.assertEqual(gen.token_shape(token), gen.JUNK)
        # Edge punctuation is the sentence's, not the token's.
        for token in ('"\'tate', "shaft.", "(vein)", "County:"):
            with self.subTest(token=token):
                self.assertEqual(gen.token_shape(token), gen.WORD)

    def test_a_line_with_nothing_judgeable_scores_zero_not_one(self):
        # An empty denominator is not a perfect score. A row of figures has no
        # word shape, and reporting 1.0 for it would make the assay tables
        # this corpus is full of look like prose.
        self.assertEqual(gen.word_shape("1,240 6,300 14.6 0.42").ratio, 0.0)
        self.assertEqual(gen.word_shape("").ratio, 0.0)

    def test_a_line_needs_four_word_shaped_tokens_not_just_a_good_ratio(self):
        # MIN_QUOTE_WORDS counts whitespace tokens, and on a table line most
        # of them are numbers. Three words among six tokens is a column of
        # figures with a label, not a sentence a reader can find again.
        line = "Northwestern Bayhorse quadrangle 1,240 6,300 14.6 0.42"
        shape = gen.word_shape(line)
        self.assertEqual(shape.ratio, 1.0)
        self.assertLess(shape.words, gen.MIN_WORD_TOKENS)
        funnel = gen.Funnel()
        self.assertEqual(gen.candidate_phrases(line, funnel), [])
        self.assertEqual(funnel.counts["phrase.implausible"], 1)


class StrataDepthTests(unittest.TestCase):
    """The yield lever: how many documents a narrow class can offer at all."""

    def test_the_query_draws_per_stratum_documents_from_each_partition(self):
        # What is measured: a --balanced run proposed 0 originals against a
        # quota of 8, and require_complete() blocks a cutover on that alone.
        # What is not: how many (state, doc_type) partitions 'originals'
        # holds. At the depth of 1 this ran at, the row count WAS that
        # partition count and --strata-limit could not raise it, so depth is
        # the only knob that CAN add rows to a narrow class -- which is the
        # case for the change, not evidence that the class was narrow.
        conn = FakeConn([document()], {SHA: [(4, 0, GOOD_LINE)]})
        gen.strata(conn, args(per_stratum=7))
        sql, params = conn.statements[0]
        self.assertIn("stratum_rank <= %s", sql)
        self.assertIn(7, params)

    def test_breadth_is_drawn_before_depth(self):
        # The reason a generous default costs a wide class nothing: the outer
        # ORDER BY takes stratum_rank first, so every partition is drawn at
        # depth 1 before any partition is drawn at depth 2, and --strata-limit
        # cuts the tail. Order by the hash alone and a deep partition could
        # take the whole budget.
        self.assertIn("ORDER BY stratum_rank, md5(sha256", gen.STRATA_SQL)

    def test_the_default_depth_is_the_one_the_diagnosis_argued_for(self):
        parsed = gen.parse_args(["--balanced", "--dsn", "postgres:///x"])
        self.assertEqual(parsed.per_stratum, gen.DEFAULT_PER_STRATUM)
        self.assertGreater(gen.DEFAULT_PER_STRATUM, 1)
        # --strata-limit is deliberately NOT the lever and stays where it was:
        # it is a ceiling the narrow class never reached, so raising it adds
        # no row to the class that needed one.
        self.assertEqual(parsed.strata_limit, 400)

    def test_a_depth_below_one_is_refused_not_read_as_an_empty_corpus(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                gen.parse_args(["--balanced", "--dsn", "postgres:///x",
                                "--per-stratum", "0"])


class ChunkSamplingTests(unittest.TestCase):
    """Which pages of a document the probes are allowed to see."""

    def test_chunks_are_sampled_across_the_document_not_off_its_front(self):
        # ORDER BY c.page, c.ordinal took the first CHUNKS_PER_DOC chunks --
        # front matter -- and MAX_PHRASE_END then restricted every probe to
        # the top quarter of each. specificity() ranks the year and the long
        # words first, which on a cover page is the letterhead: so every probe
        # was aimed at the text every sibling document from the same portal
        # shares, and is_distinctive() rejected all of it, correctly.
        self.assertNotIn("ORDER BY c.page, c.ordinal", gen.CHUNKS_SQL)
        self.assertIn("ORDER BY md5(c.sha256", gen.CHUNKS_SQL)

    def test_the_chunk_sort_key_is_injective_and_totally_ordered(self):
        # Concatenated bare, page || ordinal is not injective: (page 1,
        # ordinal 10) and (page 11, ordinal 0) build the same md5 argument, as
        # do (2, 12) and (21, 2). chunk_pages() restarts `ordinal` at 0 on
        # every page, so both members of such a pair are real rows of ONE
        # document whenever a page carries eleven chunks. A tie leaves the
        # ORDER BY without a total order -- Postgres may return either row
        # first, depending on plan shape or physical order after a VACUUM --
        # and at LIMIT 12 a tie at the boundary swaps a whole chunk in or out
        # of the sample, which is a rerun proposing a different quote.
        collide = re.search(r"md5\((.*?)\)", gen.CHUNKS_SQL, re.S).group(1)
        self.assertIn("':'", collide)
        for left, right in (((1, 10), (11, 0)), ((2, 12), (21, 2))):
            with self.subTest(pair=(left, right)):
                self.assertNotEqual(self.sort_key(*left),
                                    self.sort_key(*right))
        # And a tiebreak behind the hash, so the order is total even if two
        # distinct keys ever hashed alike.
        self.assertIn("c.page, c.ordinal", gen.CHUNKS_SQL.split("ORDER BY")[1])

    def sort_key(self, page, ordinal, sha256=SHA, seed="ws13-known-items"):
        """The md5 argument the statement builds, reconstructed in Python."""
        expression = re.search(r"md5\((.*?)\)", gen.CHUNKS_SQL,
                               re.S).group(1)
        parts = [piece.strip() for piece in expression.split("||")]
        values = {"c.sha256": sha256, "c.page::text": str(page),
                  "c.ordinal::text": str(ordinal), "%s": seed, "':'": ":"}
        return "".join(values[piece] for piece in parts)

    def test_the_sample_is_seeded_so_a_rerun_reads_the_same_pages(self):
        # Rerun-safety rests on this: a different sample every run would
        # propose different candidates over an unchanged corpus.
        conn = FakeConn([document()], {SHA: [(4, 0, GOOD_LINE)]})
        propose(conn, seed="a-particular-seed")
        chunk_calls = [params for sql, params in conn.statements
                       if "FROM ws13_chunks c" in sql]
        self.assertEqual(chunk_calls,
                         [(SHA, "a-particular-seed", gen.CHUNKS_PER_DOC)])


class ProbeSpreadTests(unittest.TestCase):
    """How the probe budget is spent across a document's pages."""

    def boilerplate(self, count):
        return [f"Bulletin {number} of the United States Geological Survey"
                for number in range(900, 900 + count)]

    def test_one_probe_on_every_page_before_a_second_on_any(self):
        # Eight probes on one page were eight draws from one distribution.
        # Interleaving makes the budget buy DIFFERENT PAGES, which is what
        # is_distinctive() rewards: the shared letterhead of a portal is on
        # page 1 of every sibling, and page 6 is not.
        #
        # This is the assertion the whole change rests on. Chunk-first
        # ordering probes a1, b1, a2, b2, ...; rank-first probes every page's
        # best line and only then comes back for the seconds.
        best = self.boilerplate(6)
        seconds = [f"The {page} adit was driven northwest along the vein for "
                   f"many feet" for page in range(1, 7)]
        chunks = [(page, 0, f"{best[page - 1]}\n{seconds[page - 1]}")
                  for page in range(1, 7)]
        conn = FakeConn([document()], {SHA: chunks},
                        {line: 40 for line in best + seconds})
        items, funnel = propose(conn)
        self.assertEqual(items, [])
        self.assertEqual(conn.probed, best + seconds)
        self.assertEqual(funnel.counts["chunk.scanned"], 6)

    def test_the_budget_covers_every_sampled_chunk(self):
        # MAX_PHRASE_PROBES is CHUNKS_PER_DOC on purpose: the sample is only
        # worth taking if every chunk in it gets a probe.
        self.assertEqual(gen.MAX_PHRASE_PROBES, gen.CHUNKS_PER_DOC)

    def test_a_running_header_does_not_cost_a_page_its_probe(self):
        # The case that defeated the interleave, and the exact case the
        # dedupe beside it was written for. `rank` used to be the phrase's
        # position in the chunk's own PRE-dedupe list, so a chunk whose best
        # line already appeared in an earlier chunk contributed no rank-0
        # entry -- and the first round-robin pass skipped that chunk
        # altogether. A running header on every page is the common shape of
        # that, not an exotic one.
        #
        # Measured before the fix, on this document: page 1 drew two probes,
        # page 12 drew none, and page 12's line was the only distinctive one
        # in the document. The header is ranked first on page 1 because
        # specificity() rewards its year and long words.
        header = ("Bulletin 927 of the United States Geological Survey "
                  "office at Boise")
        unique = [f"The number {page} adit was driven northwest along the "
                  f"vein for many hundred feet"
                  for page in range(1, gen.CHUNKS_PER_DOC + 1)]
        chunks = [(page, 0, f"{header}\n{unique[page - 1]}")
                  for page in range(1, gen.CHUNKS_PER_DOC + 1)]
        corpus = {header: 40}
        corpus.update({line: 40 for line in unique[:-1]})
        conn = FakeConn([document()], {SHA: chunks}, corpus)
        items, funnel = propose(conn)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["page"], gen.CHUNKS_PER_DOC)
        self.assertEqual(items[0]["quote"], unique[-1])
        # The header, then one line from each of the other eleven pages: no
        # page is probed twice while another is not probed at all.
        self.assertEqual(conn.probed, [header] + unique[1:])
        self.assertEqual(funnel.counts["phrase.repeat_in_doc"],
                         gen.CHUNKS_PER_DOC - 1)

    def test_a_line_repeated_in_the_chunk_overlap_is_probed_once(self):
        # ws13_worker.chunk_pages() overlaps chunks by 400 characters, so the
        # same line arrives twice. Probing it twice spends the budget on an
        # answer the corpus already gave.
        conn = FakeConn([document()],
                        {SHA: [(1, 0, GOOD_LINE), (2, 0, GOOD_LINE)]},
                        {GOOD_LINE: 40})
        items, funnel = propose(conn)
        self.assertEqual(items, [])
        self.assertEqual(conn.probes, 1)
        self.assertEqual(funnel.counts["phrase.repeat_in_doc"], 1)


class BalancedRewalkTests(unittest.TestCase):
    """The top-up pass, and what it is allowed to spend."""

    def balanced(self, conn, wanted):
        funnel = gen.Funnel()
        items = gen.propose_balanced(conn, args(balanced=True), set(), set(),
                                     set(), wanted, funnel)
        return items, funnel

    def test_the_top_up_does_not_re_probe_what_a_scoped_pass_failed(self):
        # Every stratum query selects the same partitions, so the unscoped
        # top-up walks the rows the three scoped passes just walked. A
        # document that failed on its phrases fails again identically -- every
        # filter is a pure function of the same text -- so re-probing it buys
        # nothing and costs a full budget per document.
        lines = [f"Bulletin {number} of the United States Geological Survey"
                 for number in range(900, 903)]
        docs = [document(sha256=sha, admission_class=name)
                for sha, name in zip((SHA, OTHER_SHA, THIRD_SHA),
                                     gen.ADMISSION_CLASSES)]
        chunks = {sha: [(1, 0, line)]
                  for (sha, *_), line in zip(docs, lines)}
        conn = FakeConn(docs, chunks, {line: 40 for line in lines})
        items, funnel = self.balanced(conn, 3)
        self.assertEqual(items, [])
        # Three documents, one probe each, once -- not four passes over them.
        self.assertEqual(conn.probes, 3)
        self.assertEqual(funnel.counts["doc.rewalked"], 9)
        self.assertEqual(funnel.counts["doc.no_distinctive_phrase"], 3)

    def test_a_re_walked_document_is_not_reported_as_a_rejection(self):
        # doc.rewalked is bookkeeping, not a filter, and the funnel must not
        # let it be read as the stage candidates died at.
        funnel = gen.Funnel()
        funnel.bump("doc.rewalked", 40)
        funnel.bump("doc.no_chunks", 2)
        self.assertEqual(funnel.stalled_at(), ("doc.no_chunks", 2))

    def test_a_document_already_in_the_fixture_is_bookkeeping_too(self):
        # The same argument as doc.rewalked, and leaving this bucket in was an
        # asymmetry rather than a decision: a document the fixture already
        # covers failed nothing, so naming it the widest stage sends the
        # operator to a bucket with no fix. An end-to-end run printed exactly
        # that -- "largest document-level rejection: doc.already_taken (6)".
        funnel = gen.Funnel()
        funnel.bump("doc.already_taken", 40)
        funnel.bump("doc.no_chunks", 2)
        self.assertEqual(funnel.stalled_at(), ("doc.no_chunks", 2))

    def test_a_single_pass_run_counts_no_re_walk(self):
        conn = FakeConn([document()], {SHA: [(4, 0, GOOD_LINE)]})
        items, funnel = propose(conn)
        self.assertEqual(len(items), 1)
        self.assertEqual(funnel.counts["doc.rewalked"], 0)

    def test_the_classes_still_get_their_own_quotas(self):
        # The re-walk skip must not cost the round robin its purpose: each
        # rights prefix is drawn on its own before any top-up, so a run cannot
        # come back 24 research-copies and no licensed-copies.
        docs = [document(sha256=sha, admission_class=name, state=state)
                for sha, name, state in zip((SHA, OTHER_SHA, THIRD_SHA),
                                            gen.ADMISSION_CLASSES,
                                            ("ID", "MT", "WA"))]
        chunks = {sha: [(4, 0, GOOD_LINE.replace("Lava", sha[:4]))]
                  for sha, *_ in docs}
        conn = FakeConn(docs, chunks)
        items, funnel = self.balanced(conn, 3)
        self.assertEqual(len(items), 3)
        self.assertEqual(sorted(item["admission_class"] for item in items),
                         sorted(gen.ADMISSION_CLASSES))
        self.assertEqual(funnel.counts["doc.accepted"], 3)


if __name__ == "__main__":
    unittest.main()
