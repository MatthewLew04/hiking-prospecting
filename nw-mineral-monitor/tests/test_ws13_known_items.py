"""Offline integrity gate for the Phase E known-item set.

This asserts the FIXTURE, not retrieval. tools/ws13_live_known_items.py is the
half that asks the deployed function each question; it needs a database, a
VPC and a deployed Lambda, so it cannot run here. What can run here -- with no
network, no database and no AWS -- is everything that makes the live gate
worth running at all:

  * a duplicate id, a malformed sha256 or a page of 0 means the live runner is
    checking something other than what the author intended;
  * a quote longer than the excerpt it will be searched inside can never pass,
    so the item would fail forever for a reason unrelated to retrieval;
  * a quote that is not actually on the page turns the whole gate into a test
    of the fixture. Whenever a page sidecar for an item is checked in, the
    quote is required to be a substring of it, so a fixture cannot drift away
    from the corpus without this suite going red;
  * a set in which no item asserts the vector arm certifies nothing at all.
    Setting expect_vector_source false on every item is the obvious way to
    turn a red gate green, and it silently removes the ONE assertion that
    catches a dead ANN index, so the fraction carrying it is itself checked.

What this file cannot do, and does not pretend to: distinguish a real corpus
sha256 from hashlib.sha256(b'invented').hexdigest(). Both are 64 hex
characters. The corpus cross-checks are the checked-in sidecar (available only
for public-domain originals -- 45,325 of the 56,282 documents are licensed or
research copies whose page text may not be committed to this repository) and
the live run itself.

The set is deliberately incomplete: it carries the one human-verified triple
this repository already had (the reviewed IGS IF0126 Lava Creek citation that
tools/test_doc_viewer.js asserts) and the other 24 come from the live corpus
via tools/ws13_gen_known_items.py. An incomplete set must NOT make this suite
skip -- ci/run_tests.py rejects unreviewed skips, and a skip here would hide
the shortfall instead of reporting it. So the integrity assertions run at any
size, and require_complete() is a separate function demanding all 25
verified items before cutover. infra/deploy.sh's preflight calls it when, and
only when, that deploy is the one setting WS13_RETRIEVAL_ENABLED=true -- a
deploy that leaves the flag alone is not a cutover and is not blocked by an
incomplete fixture. Until 2026-08-28 this docstring described a call that did
not exist anywhere: the gate lived only in tools/ws13_live_known_items.py, so
flipping the flag by any other route walked past it.
"""
import hashlib
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PATH = ROOT / "tests" / "fixtures" / "ws13_known_items.json"

TARGET_COUNT = 25
SHA256_RE = re.compile(r"\A[0-9a-f]{64}\Z")
REQUIRED_ITEM_KEYS = ("id", "question", "sha256", "page", "quote",
                      "expect_vector_source", "admission_class",
                      "sidecar_sha256", "verified", "provenance")
# WS12 landed exactly three rights prefixes: 10,957 originals, 13,013
# licensed-copies, 32,312 research-copies. An item may declare null when the
# corpus record was not read (the seed triple came from the WS12 manifest,
# which does not carry the WS13 admission_class), and a null is simply not
# coverage of any class -- it is never guessed at.
ADMISSION_CLASSES = ("originals", "licensed-copies", "research-copies")
# A quote is an excerpt of a page, never the page. The retrieval contract's
# default max_excerpt_chars is 760 and the window is centred on the match, so
# only about the first two thirds after the match are guaranteed; 400 keeps
# every quote checkable inside a default excerpt. Page-anchored chunks are
# ~3000 characters (CHUNK_CHARS in pipelines/ws13_worker.py), so a quote near
# that length would be a page dump rather than a locatable phrase.
MIN_QUOTE_CHARS = 12
MAX_QUOTE_CHARS = 400
# Markers that mean an item was sketched and never filled in. A fabricated
# fixture passes the live gate against itself and proves nothing.
#
# Matched as whole tokens, not substrings, so a marker inside a longer word
# is not one.
#
# 'XXX' was removed from this list rather than made a token match: in this
# corpus it is indistinguishable from content. 'Plate XXXI' survives a
# \bX{3}\b test, but 'Bulletin XXX' (roman 30) does not, and an OCR'd blanked
# form field reads as 'XXXX'. Keeping it meant discarding verified candidates
# to report an unfinished item that was never unfinished; TODO, FIXME,
# PLACEHOLDER and LOREM IPSUM say the same thing and do not collide with
# 1930s state-survey prose.
PLACEHOLDER_RE = re.compile(r"\b(?:TODO|FIXME|PLACEHOLDER|LOREM IPSUM)\b")
# The generated question is built out of the quote's own words, so an
# unrewritten template shares a long verbatim run with its quote and measures
# nothing but keyword recall. A human rewrite breaks that run; until it does,
# the item may not be marked verified. Five is the ceiling: the seed item's
# genuinely-rewritten question shares two.
MAX_SHARED_QUESTION_WORDS = 5
# The vector-arm assertion is the only thing in the system that catches a
# silently dead ANN index, so a set in which almost nothing asserts it cannot
# gate a cutover however clean the rest of the fixture is. Four fifths leaves
# room for an item a human establishes is genuinely lexical-only.
#
# A rational, not 0.8: ceil(25 * 0.8) is 21 in binary floating point, which
# would demand 21 of 25 items and be impossible to explain to whoever hit it.
VECTOR_ARM_RATIO = (4, 5)
SIDECAR_DIRNAME = "ws13_sidecars"
SIDECAR_ENV = "WS13_SIDECAR_DIR"
WORD_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9'/-]*")


def load(path=FIXTURE_PATH):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def normalize(text):
    """Collapse whitespace the way the retrieval excerpt does.

    infra/ws13_query_lambda.excerpt() runs re.sub(r"\\s+", " ", ...) before it
    windows, and OCR sidecars keep the page's line breaks, so a quote spanning
    a line break only matches once both sides are normalised.
    """
    return re.sub(r"\s+", " ", text or "").strip()


def content_words(text):
    """The words tools/ws13_gen_known_items.question_for() copies.

    That template keeps every word longer than three characters, in order, so
    this is exactly the sequence a rewritten question has to break.
    """
    return [word.lower() for word in WORD_RE.findall(text or "") if len(word) > 3]


def _contains_run(haystack, run):
    span = len(run)
    return any(haystack[start:start + span] == run
               for start in range(len(haystack) - span + 1))


def longest_shared_run(question, quote):
    """Longest run of the question's content words that is also a contiguous
    run of the quote's, in order and case-insensitively."""
    haystack, needles = content_words(quote), content_words(question)
    best = 0
    for start in range(len(needles)):
        for end in range(start + best + 1, len(needles) + 1):
            if not _contains_run(haystack, needles[start:end]):
                break
            best = end - start
    return best


def sidecar_roots(extra=None):
    """Directories searched for a checked-in page sidecar, most specific first."""
    roots = list(extra or [])
    from_env = os.environ.get(SIDECAR_ENV, "").strip()
    if from_env:
        roots.append(Path(from_env))
    roots.append(ROOT / "tests" / "fixtures" / SIDECAR_DIRNAME)
    return [Path(root) for root in roots]


def sidecar_path(sha256, page, roots=None):
    """The sidecar for one (sha256, page), or None when none is checked in."""
    for root in sidecar_roots(roots):
        for candidate in (root / f"{sha256}.p{int(page):04d}.txt",
                          root / sha256 / f"page-{int(page):04d}.txt"):
            if candidate.is_file():
                return candidate
    return None


def _sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def item_problems(item, roots=None):
    """Everything wrong with one item, as a list of strings."""
    problems = []
    label = item.get("id") or "<no id>"
    for key in REQUIRED_ITEM_KEYS:
        if key not in item:
            problems.append(f"{label}: missing required key {key!r}")
    if problems:
        return problems

    if not isinstance(item["id"], str) or not item["id"].strip():
        problems.append(f"{label}: id must be a non-empty string")
    if not isinstance(item["question"], str) or len(item["question"].strip()) < 12:
        problems.append(f"{label}: question is missing or too short to be a query")

    sha256 = item["sha256"]
    if not isinstance(sha256, str) or not SHA256_RE.match(sha256):
        problems.append(f"{label}: sha256 {sha256!r} is not 64 lowercase hex "
                        "characters")
    elif len(set(sha256)) <= 2:
        problems.append(f"{label}: sha256 {sha256!r} looks synthesised; a "
                        "known item must name a real document")

    page = item["page"]
    if isinstance(page, bool) or not isinstance(page, int) or page < 1:
        problems.append(f"{label}: page {page!r} must be a positive integer")

    quote = item["quote"]
    if not isinstance(quote, str) or not quote.strip():
        problems.append(f"{label}: quote is empty")
    else:
        if quote != normalize(quote):
            problems.append(f"{label}: quote is not whitespace-normalised; "
                            "store the form the excerpt will produce")
        if len(quote) < MIN_QUOTE_CHARS:
            problems.append(f"{label}: quote is {len(quote)} characters, under "
                            f"the {MIN_QUOTE_CHARS}-character floor and too "
                            "generic to identify a page")
        if len(quote) > MAX_QUOTE_CHARS:
            problems.append(f"{label}: quote is {len(quote)} characters, over "
                            f"the {MAX_QUOTE_CHARS}-character ceiling; it "
                            "could not fit inside a 760-character excerpt")

    if not isinstance(item["expect_vector_source"], bool):
        problems.append(f"{label}: expect_vector_source must be a boolean")
    admission_class = item["admission_class"]
    if admission_class is not None and admission_class not in ADMISSION_CLASSES:
        problems.append(f"{label}: admission_class {admission_class!r} is not "
                        f"one of {ADMISSION_CLASSES} or null")
    if not isinstance(item["verified"], bool):
        problems.append(f"{label}: verified must be a boolean")
    if item["verified"] and isinstance(item["question"], str) and isinstance(
            quote, str):
        shared = longest_shared_run(item["question"], quote)
        if shared > MAX_SHARED_QUESTION_WORDS:
            problems.append(
                f"{label}: the question repeats {shared} of the quote's own "
                "words in order; that is the generator's template, which only "
                "proves the lexical arm works. Rewrite it before setting "
                "verified")
    if not isinstance(item["provenance"], str) or len(
            item["provenance"].strip()) < 20:
        problems.append(f"{label}: provenance must say where the triple came "
                        "from, specifically enough to re-check")

    sidecar_sha = item["sidecar_sha256"]
    if sidecar_sha is not None and (
            not isinstance(sidecar_sha, str) or not SHA256_RE.match(sidecar_sha)):
        problems.append(f"{label}: sidecar_sha256 must be null or 64 lowercase "
                        "hex characters")

    haystack = " ".join(str(item.get(key) or "") for key in
                        ("id", "question", "quote", "provenance")).upper()
    for marker in sorted(set(PLACEHOLDER_RE.findall(haystack))):
        problems.append(f"{label}: contains the placeholder marker "
                        f"{marker!r}; an unfinished item must not ship")

    problems.extend(sidecar_problems(item, roots=roots))
    return problems


def sidecar_problems(item, roots=None):
    """Quote-versus-sidecar drift, when a sidecar is checked in locally."""
    problems = []
    label = item.get("id") or "<no id>"
    sha256 = str(item.get("sha256") or "")
    page = item.get("page")
    if not SHA256_RE.match(sha256) or not isinstance(page, int) or page < 1:
        return problems           # already reported by item_problems
    path = sidecar_path(sha256, page, roots=roots)
    declared = item.get("sidecar_sha256")
    if path is None:
        if declared:
            # Fail closed: an item that names a sidecar digest and has no
            # sidecar file has drifted away from whatever it was checked
            # against, and nothing here can tell how far.
            problems.append(f"{label}: sidecar_sha256 is set but no sidecar "
                            f"for {sha256[:12]} page {page} is present")
        return problems
    text = normalize(path.read_text(encoding="utf-8", errors="replace"))
    if normalize(item.get("quote")) not in text:
        problems.append(f"{label}: quote is not in the checked-in sidecar "
                        f"{path.name}; the fixture has drifted from the corpus")
    if declared:
        actual = _sha256_file(path)
        if actual != declared:
            problems.append(f"{label}: sidecar {path.name} hashes to "
                            f"{actual[:12]}, not the declared {declared[:12]}")
    return problems


def min_vector_items(count):
    """How many of `count` items must assert the vector arm, rounded up."""
    numerator, denominator = VECTOR_ARM_RATIO
    return -(-int(count) * numerator // denominator) if count else 0


def vector_arm_problems(items):
    """Too few items assert the vector arm for the gate to mean anything.

    This is the failure the rest of the file cannot see: every other check
    passes on a set whose expect_vector_source is false everywhere, and
    tools/ws13_live_known_items.judge() then adds no arm reason to any item,
    so 25 lexical hits certify a cutover while ws13_chunks_titan_hnsw does not
    exist and arms.vector.enabled is false on every response.
    """
    items = list(items or [])
    if not items:
        return []
    asserting = sum(1 for item in items
                    if item.get("expect_vector_source") is True)
    needed = min_vector_items(len(items))
    if asserting >= needed:
        return []
    return [f"only {asserting} of {len(items)} item(s) set "
            f"expect_vector_source; at least {needed} "
            f"({VECTOR_ARM_RATIO[0]}/{VECTOR_ARM_RATIO[1]} of the set) must, "
            "because that assertion is the only one that fails when the "
            "vector arm is silently dead"]


def class_coverage_problems(items):
    """A complete set has to span all three rights prefixes.

    --balanced draws its strata at random with no per-class quota, so a run
    can legitimately return 24 research-copies and zero licensed-copies,
    leaving the CC BY-NC-SA prefix -- the one carrying share-alike and
    non-commercial obligations -- untested by the gate.
    """
    declared = {item.get("admission_class") for item in items or []}
    missing = [name for name in ADMISSION_CLASSES if name not in declared]
    if not missing:
        return []
    return [f"no item covers admission_class {', '.join(missing)}; the gate "
            "would certify a cutover with a rights prefix it never retrieved"]


def integrity_problems(fixture=None, roots=None):
    """Everything wrong with the set, independent of how complete it is."""
    fixture = load() if fixture is None else fixture
    problems = []
    if fixture.get("schema_version") != 1:
        problems.append(f"schema_version is {fixture.get('schema_version')!r}, "
                        "not 1")
    if fixture.get("target_count") != TARGET_COUNT:
        problems.append(f"target_count is {fixture.get('target_count')!r}, not "
                        f"{TARGET_COUNT}")
    items = fixture.get("items")
    if not isinstance(items, list):
        return problems + ["items is missing or is not a list"]
    if len(items) > TARGET_COUNT:
        problems.append(f"{len(items)} items exceed the target of "
                        f"{TARGET_COUNT}")

    seen_ids, seen_triples = set(), set()
    for item in items:
        problems.extend(item_problems(item, roots=roots))
        identifier = item.get("id")
        if identifier in seen_ids:
            problems.append(f"duplicate id {identifier!r}")
        seen_ids.add(identifier)
        triple = (item.get("sha256"), item.get("page"),
                  normalize(item.get("quote")))
        if triple in seen_triples:
            problems.append(f"duplicate (sha256, page, quote) triple for "
                            f"{identifier!r}; it would measure one document "
                            "twice and inflate recall")
        seen_triples.add(triple)
    problems.extend(vector_arm_problems(items))
    return problems


def require_complete(fixture=None, roots=None):
    """The cutover gate: every problem that must be zero before the canary.

    The deploy preflight calls this. It is deliberately separate from the
    integrity assertions so an incomplete set reports its shortfall in CI
    rather than skipping, and so nobody can cut over on 1 of 25 items.
    """
    fixture = load() if fixture is None else fixture
    problems = integrity_problems(fixture, roots=roots)
    items = fixture.get("items") or []
    target = fixture.get("target_count") or TARGET_COUNT
    if len(items) < target:
        problems.append(
            f"known-item set is short: {len(items)} of {target} present, "
            f"{target - len(items)} still to be generated by "
            "tools/ws13_gen_known_items.py and confirmed by a human")
    unverified = sorted(str(item.get("id")) for item in items
                        if not item.get("verified"))
    if unverified:
        problems.append(f"{len(unverified)} item(s) are not human-verified: "
                        f"{', '.join(unverified)}")
    asserting = sum(1 for item in items
                    if item.get("expect_vector_source") is True)
    needed = min_vector_items(target)
    if asserting < needed:
        problems.append(
            f"only {asserting} item(s) assert the vector arm; a cutover needs "
            f"at least {needed} of {target}. Without them the set passes on "
            "lexical hits alone and certifies a vector arm that never ran")
    problems.extend(class_coverage_problems(items))
    if not fixture.get("complete"):
        problems.append("fixture 'complete' is false; it flips to true only "
                        "once every item is present and verified")
    return problems


def synthetic_fixture(count=TARGET_COUNT, complete=True):
    """A well-formed in-memory set, so the gate can be proven to pass.

    The digests come from hashlib over a fixed label: real 64-hex strings that
    are obviously not corpus documents. Nothing here is ever written to the
    checked-in fixture.
    """
    items = []
    for index in range(count):
        digest = hashlib.sha256(f"ws13-known-item-self-test-{index}".encode()
                                ).hexdigest()
        items.append({
            "id": f"self-test-{index:02d}",
            "question": f"What does self-test document {index} record?",
            "sha256": digest, "page": index + 1,
            "quote": f"self test phrase number {index} on its own page",
            "expect_vector_source": True,
            "admission_class": ADMISSION_CLASSES[index % len(ADMISSION_CLASSES)],
            "sidecar_sha256": None,
            "verified": True,
            "provenance": "synthetic in-memory fixture for the gate self-test",
        })
    return {"schema_version": 1, "target_count": TARGET_COUNT,
            "complete": complete, "items": items}


def live_runner():
    """tools/ws13_live_known_items.py, imported on demand.

    Not at module scope: that tool imports this module by name, and a fresh
    copy of this module would then import the half-initialised tool back.
    Deferring the import to call time breaks the cycle in every discovery
    layout.
    """
    sys.path.insert(0, str(ROOT / "tools"))
    import ws13_live_known_items

    return ws13_live_known_items


def generator():
    """tools/ws13_gen_known_items.py, imported on demand.

    It imports psycopg, which is a deployment dependency of the in-VPC host
    and not of the test host, so a stub stands in for the name the same way
    tests/test_ws13_retrieval.py stubs it. Nothing here connects to anything.
    """
    if "psycopg" not in sys.modules:
        try:
            import psycopg                                # noqa: F401
        except ImportError:
            import types

            sys.modules["psycopg"] = types.ModuleType("psycopg")
    sys.path.insert(0, str(ROOT / "tools"))
    import ws13_gen_known_items

    return ws13_gen_known_items


class FixtureIntegrityTest(unittest.TestCase):
    def setUp(self):
        self.fixture = load()
        self.items = self.fixture["items"]

    def test_the_fixture_is_structurally_sound_at_its_current_size(self):
        self.assertEqual(integrity_problems(self.fixture), [])

    def test_target_count_is_twenty_five(self):
        self.assertEqual(self.fixture["target_count"], TARGET_COUNT)

    def test_the_set_never_exceeds_its_target(self):
        """Incomplete is expected and is not a skip; overfull is a mistake."""
        self.assertLessEqual(len(self.items), TARGET_COUNT)
        self.assertGreaterEqual(len(self.items), 1)

    def test_ids_are_unique(self):
        identifiers = [item["id"] for item in self.items]
        self.assertEqual(len(identifiers), len(set(identifiers)))

    def test_every_sha256_is_sixty_four_lowercase_hex(self):
        for item in self.items:
            with self.subTest(item=item["id"]):
                self.assertRegex(item["sha256"], SHA256_RE)

    def test_every_page_is_a_positive_integer(self):
        for item in self.items:
            with self.subTest(item=item["id"]):
                self.assertIsInstance(item["page"], int)
                self.assertNotIsInstance(item["page"], bool)
                self.assertGreaterEqual(item["page"], 1)

    def test_every_quote_is_a_phrase_not_a_page(self):
        for item in self.items:
            with self.subTest(item=item["id"]):
                quote = item["quote"]
                self.assertTrue(quote.strip())
                self.assertGreaterEqual(len(quote), MIN_QUOTE_CHARS)
                self.assertLessEqual(len(quote), MAX_QUOTE_CHARS)
                self.assertEqual(quote, normalize(quote))

    def test_no_duplicate_sha_page_quote_triples(self):
        triples = [(item["sha256"], item["page"], normalize(item["quote"]))
                   for item in self.items]
        self.assertEqual(len(triples), len(set(triples)))

    def test_the_seed_item_is_the_reviewed_if0126_lava_creek_citation(self):
        """The one triple this repository already carried, human-reviewed for
        WS12 and asserted by tools/test_doc_viewer.js."""
        seed = self.items[0]
        self.assertEqual(seed["page"], 1)
        self.assertIn("LAVA CREEK DISTRICT", seed["quote"])
        self.assertTrue(seed["verified"])
        self.assertEqual(
            seed["sha256"],
            "3c3fc7e970db5286640b83c35e886fc07a6db4c415e92782674aa94a3058a9a1")
        viewer = (ROOT / "tools" / "test_doc_viewer.js").read_text(
            encoding="utf-8")
        self.assertIn("LAVA CREEK DISTRICT", viewer,
                      "the browser acceptance no longer asserts the citation "
                      "this fixture was copied from")

    def test_the_fixture_documents_its_own_schema(self):
        """A gate nobody can read is a gate nobody maintains."""
        for key in ("schema_version", "target_count", "complete", "provenance",
                    "item_schema", "items"):
            self.assertIn(key, self.fixture)
        for key in REQUIRED_ITEM_KEYS:
            self.assertIn(key, self.fixture["item_schema"])

    def test_provenance_names_the_generator_for_the_missing_items(self):
        self.assertIn("ws13_gen_known_items.py", self.fixture["provenance"])
        self.assertFalse(self.fixture["complete"])

    def test_the_present_items_already_assert_the_vector_arm(self):
        """Checked at the current size, not only at the cutover gate: a set
        that stops asserting the arm has stopped being able to fail."""
        self.assertEqual(vector_arm_problems(self.items), [])
        self.assertGreaterEqual(
            sum(1 for item in self.items
                if item["expect_vector_source"] is True),
            min_vector_items(len(self.items)))

    def test_every_admission_class_is_a_known_prefix_or_null(self):
        for item in self.items:
            with self.subTest(item=item["id"]):
                self.assertIn(item["admission_class"],
                              (None,) + ADMISSION_CLASSES)


class PlaceholderMarkerTest(unittest.TestCase):
    """A marker means unfinished; a roman numeral means thirty.

    Roman numerals and OCR'd blanked form fields are ordinary in this corpus,
    and rejecting them cost a verified candidate its place in the set for a
    placeholder that was never there.
    """

    def item(self, **changes):
        base = {
            "id": "marker-case", "question": "What does the page record here?",
            "sha256": hashlib.sha256(b"marker-case").hexdigest(), "page": 2,
            "quote": "a phrase long enough to identify one page of one file",
            "expect_vector_source": True, "admission_class": "originals",
            "sidecar_sha256": None, "verified": True,
            "provenance": "temporary fixture for the marker-token self-test",
        }
        base.update(changes)
        return base

    def test_a_bare_marker_token_is_still_rejected(self):
        for text in ("TODO confirm this quote", "FIXME", "PLACEHOLDER text",
                     "lorem ipsum dolor sit"):
            with self.subTest(text=text):
                problems = item_problems(self.item(
                    quote=f"{text} and enough more words to pass the floor"))
                self.assertTrue(any("placeholder marker" in problem
                                    for problem in problems), problems)

    def test_a_marker_inside_a_longer_word_is_not_one(self):
        self.assertEqual(item_problems(self.item(
            question="Which mastodon fossils are recorded on this page?")), [])

    def test_roman_numerals_and_redacted_runs_are_not_markers(self):
        for text in ("Plate XXXI shows the vein", "Bulletin XXX, page four",
                     "operator name XXXX redacted on the filed form",
                     "XXXVIII claims were located that season"):
            with self.subTest(text=text):
                self.assertEqual(
                    item_problems(self.item(quote=f"{text} in the district")),
                    [])


class SidecarDriftTest(unittest.TestCase):
    """Proves the sidecar check actually checks something.

    No WS13 page sidecar is checked into this repository yet, so on a clean
    checkout the sidecar branch of item_problems() would never execute. These
    drive it against a temporary directory instead, so the mechanism is
    covered offline without inventing a corpus artifact.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.sha = hashlib.sha256(b"sidecar-drift-test").hexdigest()
        self.item = {
            "id": "sidecar-case", "question": "What does the page record?",
            "sha256": self.sha, "page": 4,
            "quote": "Mining District Name: LAVA CREEK DISTRICT County: BUTTE",
            "expect_vector_source": True, "sidecar_sha256": None,
            "verified": True,
            "provenance": "temporary fixture for the sidecar drift self-test",
        }

    def write_sidecar(self, body, layout="flat"):
        if layout == "flat":
            path = self.root / f"{self.sha}.p0004.txt"
        else:
            path = self.root / self.sha / "page-0004.txt"
            path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        return path

    def test_a_matching_quote_passes_across_a_line_break(self):
        """The real IF0126 sidecar puts 'LAVA CREEK DISTRICT' and
        'County: BUTTE' on separate lines; the reviewed quote joins them."""
        self.write_sidecar("MRDS-W015681\nST. LOUIS MINE\n"
                           "Mining District Name: LAVA CREEK DISTRICT\n"
                           "County: BUTTE\nUTM Zone: +12\n")
        self.assertEqual(sidecar_problems(self.item, roots=[self.root]), [])

    def test_the_nested_sidecar_layout_is_found_too(self):
        self.write_sidecar("Mining District Name: LAVA CREEK DISTRICT\n"
                           "County: BUTTE\n", layout="nested")
        self.assertEqual(sidecar_problems(self.item, roots=[self.root]), [])

    def test_a_quote_that_is_not_on_the_page_is_caught(self):
        self.write_sidecar("Mining District Name: YANKEE FORK DISTRICT\n"
                           "County: CUSTER\n")
        problems = sidecar_problems(self.item, roots=[self.root])
        self.assertEqual(len(problems), 1)
        self.assertIn("drifted from the corpus", problems[0])

    def test_a_declared_sidecar_digest_must_match_the_file(self):
        path = self.write_sidecar("Mining District Name: LAVA CREEK DISTRICT\n"
                                  "County: BUTTE\n")
        item = dict(self.item, sidecar_sha256=_sha256_file(path))
        self.assertEqual(sidecar_problems(item, roots=[self.root]), [])
        wrong = dict(self.item, sidecar_sha256=hashlib.sha256(b"other"
                                                              ).hexdigest())
        problems = sidecar_problems(wrong, roots=[self.root])
        self.assertEqual(len(problems), 1)
        self.assertIn("hashes to", problems[0])

    def test_a_declared_digest_with_no_sidecar_file_fails_closed(self):
        item = dict(self.item, sidecar_sha256=hashlib.sha256(b"x").hexdigest())
        problems = sidecar_problems(item, roots=[self.root])
        self.assertEqual(len(problems), 1)
        self.assertIn("no sidecar", problems[0])

    def test_no_sidecar_and_no_declared_digest_is_not_a_problem(self):
        self.assertEqual(sidecar_problems(self.item, roots=[self.root]), [])


class CompletenessGateTest(unittest.TestCase):
    """require_complete() is what gates a cutover.

    infra/deploy.sh runs it from preflight when the deploy sets
    WS13_RETRIEVAL_ENABLED=true, and tools/ws13_live_known_items.py runs it
    before a certifying run.
    """

    def test_it_reports_the_current_shortfall_honestly(self):
        fixture = load()
        problems = require_complete(fixture)
        self.assertTrue(problems, "the set is not complete; saying so is the "
                                  "whole point of this function")
        present = len(fixture["items"])
        missing = TARGET_COUNT - present
        shortfall = [text for text in problems if "known-item set is short"
                     in text]
        self.assertEqual(len(shortfall), 1)
        self.assertIn(f"{present} of {TARGET_COUNT} present", shortfall[0])
        self.assertIn(f"{missing} still to be generated", shortfall[0])
        self.assertTrue(any("'complete' is false" in text for text in problems))
        # The shortfall must be the ONLY thing wrong: an integrity failure
        # hiding behind an incomplete set is how a bad item ships later.
        self.assertEqual(integrity_problems(fixture), [])

    def test_it_returns_nothing_for_a_complete_verified_set(self):
        """Otherwise the gate could never be satisfied and would be ignored."""
        self.assertEqual(require_complete(synthetic_fixture()), [])

    def test_it_refuses_a_full_set_that_is_not_yet_verified(self):
        fixture = synthetic_fixture()
        fixture["items"][7]["verified"] = False
        problems = require_complete(fixture)
        self.assertTrue(any("not human-verified" in text for text in problems))
        self.assertTrue(any("self-test-07" in text for text in problems))

    def test_it_refuses_a_full_set_still_flagged_incomplete(self):
        problems = require_complete(synthetic_fixture(complete=False))
        self.assertEqual(len(problems), 1)
        self.assertIn("'complete' is false", problems[0])

    def test_it_refuses_a_set_with_a_duplicate_item(self):
        fixture = synthetic_fixture()
        fixture["items"][3] = dict(fixture["items"][2],
                                   id="self-test-03-duplicate")
        problems = require_complete(fixture)
        self.assertTrue(any("duplicate (sha256, page, quote)" in text
                            for text in problems))

    def test_it_refuses_a_set_with_a_degenerate_sha256(self):
        """Only the DEGENERATE case is catchable here, and saying so is part
        of the test: '0'*64 and 'abababab...' are rejected, but
        hashlib.sha256(b'invented').hexdigest() is indistinguishable from a
        corpus digest offline -- synthetic_fixture() relies on exactly that.
        A checked-in sidecar is the only local corpus cross-check, and it is
        available only for the 10,957 public-domain originals; the other
        45,325 documents are licensed or research copies whose page text may
        not be committed here. For those, the live run is the attestation."""
        fixture = synthetic_fixture()
        fixture["items"][0]["sha256"] = "0" * 64
        problems = require_complete(fixture)
        self.assertTrue(any("looks synthesised" in text for text in problems))
        invented = hashlib.sha256(b"invented").hexdigest()
        self.assertEqual(item_problems(dict(fixture["items"][1],
                                            sha256=invented)), [])

    def test_it_refuses_a_set_where_nothing_asserts_the_vector_arm(self):
        """Clearing expect_vector_source everywhere is the obvious way to turn
        a red gate green: every other check still passes, judge() stops adding
        an arm reason, and 25 lexical hits certify a cutover with
        ws13_chunks_titan_hnsw absent."""
        fixture = synthetic_fixture()
        for item in fixture["items"]:
            item["expect_vector_source"] = False
        problems = require_complete(fixture)
        self.assertTrue(any("assert the vector arm" in text
                            for text in problems), problems)
        # It is also an integrity failure, so it surfaces at any set size
        # rather than only at the cutover gate.
        self.assertTrue(any("expect_vector_source" in text for text in
                            integrity_problems(fixture)))

    def test_it_allows_a_minority_of_lexical_only_items(self):
        """The flag has to stay usable for an item a human establishes is
        genuinely lexical-only, or it becomes a constant nobody can set."""
        fixture = synthetic_fixture()
        for item in fixture["items"][:5]:
            item["expect_vector_source"] = False
        self.assertEqual(require_complete(fixture), [])
        fixture["items"][5]["expect_vector_source"] = False
        self.assertTrue(any("assert the vector arm" in text for text in
                            require_complete(fixture)))

    def test_it_refuses_a_set_that_misses_a_rights_prefix(self):
        fixture = synthetic_fixture()
        for item in fixture["items"]:
            if item["admission_class"] == "licensed-copies":
                item["admission_class"] = "originals"
        problems = require_complete(fixture)
        self.assertTrue(any("licensed-copies" in text for text in problems),
                        problems)

    def test_it_refuses_a_verified_item_still_carrying_the_template_question(self):
        """A reviewer who flips verified across 24 untouched generated items
        produces a set that measures keyword recall and nothing else."""
        fixture = synthetic_fixture()
        item = fixture["items"][2]
        item["quote"] = ("exposed a silver-lead shoot averaging fourteen "
                         "ounces per ton across three feet")
        item["question"] = generator().question_for("Report 12", item["quote"])
        problems = require_complete(fixture)
        self.assertTrue(any("repeats" in text and "words in order" in text
                            for text in problems), problems)
        # The same item is fine while it is honestly unverified: the generator
        # emits templates and they still have to pass the integrity gate.
        item["verified"] = False
        self.assertEqual(integrity_problems(fixture), [])


class LiveRunnerVerdictTest(unittest.TestCase):
    """tools/ws13_live_known_items.judge(), driven on canned responses.

    The runner needs a VPC, a database and a deployed Lambda; judge() needs
    neither, and it is where a correct retrieval was being scored as a miss.
    """

    def setUp(self):
        self.runner = live_runner()
        self.item = {
            "id": "multi-chunk-page",
            "sha256": hashlib.sha256(b"multi-chunk-page").hexdigest(),
            "page": 7,
            "quote": "averaging fourteen ounces of silver per ton",
        }

    def hit(self, chunk_id, excerpt, ordinal=0, sources=("lexical",),
            sha256=None, page=7):
        return {"chunk_id": chunk_id, "sha256": sha256 or self.item["sha256"],
                "page": page, "ordinal": ordinal, "excerpt": excerpt,
                "sources": list(sources)}

    def response(self, hits, enabled=True, reason=None):
        return {"status": "loaded", "retrieval_mode": "rrf_lexical_vector",
                "hits": hits,
                "arms": {"lexical": {"candidates": len(hits)},
                         "vector": {"candidates": len(hits),
                                    "enabled": enabled, "reason": reason}}}

    def test_the_quote_is_found_in_any_chunk_of_the_right_page(self):
        """ws13_chunks is keyed (sha256, page, ordinal) and a page over 3,000
        characters is several chunks, so the hit carrying the quote is not
        always the one that ranked first. Anchoring on the first match failed
        a retrieval that was correct."""
        hits = [self.hit(1, "the shoot was opened from the upper adit",
                         ordinal=0, sources=("lexical", "vector")),
                self.hit(2, "later work reported ore averaging fourteen "
                            "ounces of silver per ton across the stope",
                         ordinal=1, sources=("vector",))]
        verdict = self.runner.judge(self.item, self.response(hits), 5, True)
        self.assertEqual(verdict["reasons"], [])
        self.assertTrue(verdict["passed"])
        self.assertTrue(verdict["quote_found"])
        # The report anchors on the chunk that actually answered.
        self.assertEqual(verdict["anchor_chunk_id"], 2)

    def test_a_page_whose_chunks_all_miss_the_quote_still_fails(self):
        hits = [self.hit(1, "the shoot was opened from the upper adit"),
                self.hit(2, "no assay is recorded for this level", ordinal=1)]
        verdict = self.runner.judge(self.item, self.response(hits), 5, True)
        self.assertFalse(verdict["passed"])
        self.assertTrue(any("quote is in none" in reason
                            for reason in verdict["reasons"]))
        self.assertFalse(verdict["quote_found"])

    def test_the_wrong_page_is_not_a_hit_even_in_the_right_document(self):
        hits = [self.hit(1, "averaging fourteen ounces of silver per ton",
                         page=8)]
        verdict = self.runner.judge(self.item, self.response(hits), 5, True)
        self.assertFalse(verdict["anchor_found"])
        self.assertTrue(any("no top-5 hit" in reason
                            for reason in verdict["reasons"]))

    def test_a_dead_vector_arm_is_a_failure_not_a_ranking_outcome(self):
        hits = [self.hit(1, "ore averaging fourteen ounces of silver per ton")]
        verdict = self.runner.judge(
            self.item,
            self.response(hits, enabled=False, reason="index missing"),
            5, True)
        self.assertFalse(verdict["passed"])
        self.assertTrue(any("vector arm" in reason
                            for reason in verdict["reasons"]))


class LiveRunnerCertificationTest(unittest.TestCase):
    """certify(): what must be true of the RUN, not of one item."""

    def setUp(self):
        self.runner = live_runner()

    def summary(self, **changes):
        results = [{"id": f"item-{index}", "passed": True,
                    "vector_sourced": True,
                    "vector_arm": {"enabled": True, "reason": None}}
                   for index in range(3)]
        summary = {"items": len(results), "passed": len(results),
                   "vector_arm_rate": 1.0, "results": results}
        summary["vector_arm_disabled"] = self.runner.arm_disabled_in(results)
        summary.update(changes)
        return summary

    def test_a_clean_run_certifies(self):
        self.assertEqual(self.runner.certify(self.summary()), [])

    def test_a_run_that_never_saw_the_vector_arm_cannot_certify(self):
        """Clearing expect_vector_source on every item removes the per-item
        assertion; this is the run-level backstop, and it is what refuses a
        cutover while ws13_chunks_titan_hnsw does not exist."""
        summary = self.summary()
        for result in summary["results"]:
            result["vector_sourced"] = False
        summary["vector_arm_rate"] = 0.0
        blocking = self.runner.certify(summary)
        self.assertTrue(any("vector_arm_rate 0.0" in reason
                            for reason in blocking), blocking)

    def test_a_disabled_arm_blocks_even_when_every_item_passed(self):
        summary = self.summary()
        summary["results"][1]["vector_arm"] = {
            "enabled": False, "reason": "index ws13_chunks_titan_hnsw does "
                                        "not exist"}
        summary["vector_arm_disabled"] = self.runner.arm_disabled_in(
            summary["results"])
        blocking = self.runner.certify(summary)
        self.assertTrue(any("reported disabled" in reason
                            for reason in blocking), blocking)
        self.assertIn("item-1", " ".join(blocking))

    def test_a_failed_item_is_named(self):
        summary = self.summary()
        summary["results"][2]["passed"] = False
        blocking = self.runner.certify(summary)
        self.assertTrue(any("item-2" in reason for reason in blocking))


class GeneratedCandidateTest(unittest.TestCase):
    """tools/ws13_gen_known_items.py, over text instead of over Postgres.

    Everything asserted here is a pure function of one chunk's text; nothing
    connects, and psycopg is only a name the module imports.
    """

    def setUp(self):
        self.gen = generator()

    def chunk(self, tail_offset):
        """A chunk whose distinctive sentence starts at about `tail_offset`."""
        filler = "The district was examined again in the following season. "
        head = filler * max(1, round(tail_offset / len(filler)))
        return head + ("The upper adit exposed a silver-lead shoot averaging "
                       "fourteen ounces of silver per ton.")

    def test_a_phrase_inside_the_excerpt_window_is_offered(self):
        phrases = dict(self.gen.candidate_phrases(self.chunk(120)))
        self.assertIn("The upper adit exposed a silver-lead shoot averaging "
                      "fourteen ounces of silver per ton.", phrases)

    def test_a_phrase_past_the_excerpt_window_is_dropped(self):
        """The live gate only ever sees the leading max_excerpt_chars of a
        chunk, and CHUNK_CHARS is 3000. A quote at offset 2,000 fails
        judge() forever on a retrieval that was in fact correct."""
        phrases = dict(self.gen.candidate_phrases(self.chunk(2000)))
        self.assertNotIn("The upper adit exposed a silver-lead shoot "
                         "averaging fourteen ounces of silver per ton.",
                         phrases)
        for phrase, offset in self.gen.candidate_phrases(self.chunk(2000)):
            with self.subTest(offset=offset):
                self.assertLessEqual(offset + len(phrase),
                                     self.gen.MAX_PHRASE_END)

    def test_every_offset_locates_the_phrase_in_the_normalised_chunk(self):
        """The offset is in the whitespace-normalised chunk because that is
        what infra/ws13_query_lambda.excerpt() windows."""
        text = self.chunk(200).replace(". ", ".\n   ")
        flat = self.gen.normalize(text)
        for phrase, offset in self.gen.candidate_phrases(text):
            with self.subTest(phrase=phrase[:32]):
                self.assertEqual(flat[offset:offset + len(phrase)], phrase)

    def test_the_generated_question_is_a_template_the_gate_will_not_verify(self):
        quote = ("The upper adit exposed a silver-lead shoot averaging "
                 "fourteen ounces of silver per ton.")
        question = self.gen.question_for("Mineral Resources of Custer County",
                                         quote)
        self.assertGreater(longest_shared_run(question, quote),
                           MAX_SHARED_QUESTION_WORDS)
        candidate = {
            "id": "gen-template", "question": question,
            "sha256": hashlib.sha256(b"gen-template").hexdigest(), "page": 3,
            "quote": quote, "expect_vector_source": True,
            "admission_class": "originals", "sidecar_sha256": None,
            "verified": False,
            "provenance": "generated by tools/ws13_gen_known_items.py for the "
                          "template self-test",
        }
        # Unverified, it must pass: the generator emits templates and they
        # still have to clear the integrity gate on the way in.
        self.assertEqual(item_problems(candidate), [])
        candidate["verified"] = True
        self.assertTrue(any("repeats" in problem
                            for problem in item_problems(candidate)))

    def test_the_generator_shares_this_module_s_definition_of_a_good_item(self):
        """The tool validates every candidate against item_problems() before
        writing, so it cannot emit a fixture that turns CI red."""
        source = (ROOT / "tools" / "ws13_gen_known_items.py").read_text(
            encoding="utf-8")
        self.assertIn("from test_ws13_known_items import item_problems", source)
        self.assertIs(self.gen.item_problems, item_problems)


if __name__ == "__main__":
    unittest.main(verbosity=2)
