"""tools/ws13_verify_worksheet.py -- the step-1 worksheet builder.

What these pin, and why each one is here rather than being obvious:

  * A fragment is only emitted when the citation carries an s3_key. The WS13
    corpus is indexed in a Postgres the docs API cannot read, so viewer.html
    is passed the stored original's key; a fragment without one resolves
    nothing, and guessing a key shape would be inventing a private object's
    name.
  * A verified item is never re-rendered. The worksheet exists to be worked
    through, and re-presenting finished items invites re-doing them.
  * The quote and question are escaped. They are OCR text from scanned mine
    files -- angle brackets and ampersands are ordinary content there, not a
    hypothetical.

No AWS: lookup() is injected or disabled throughout.
"""
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

import ws13_verify_worksheet as worksheet          # noqa: E402

SITE = 'https://example.invalid'
CITATION = {
    's3_key': 'ws12/research-copies/nbmg/95/95bd.pdf',
    'rights_basis': 'NBMG scan; research copy',
    'viewer_key_kind': 'searchable',
    'document_title': 'THE COMSTOCK',
}
ITEM = {'id': 'res-nv-95bd-p2', 'sha256': 'a' * 64, 'page': 2,
        'quote': 'a distinctive line from the page',
        'question': 'In "x.pdf", what is recorded about a distinctive line?',
        'admission_class': 'research-copies', 'verified': False}


class ViewerUrlTests(unittest.TestCase):
    def test_the_fragment_carries_what_the_viewer_needs(self):
        url = worksheet.viewer_url(SITE, ITEM, CITATION)
        self.assertTrue(url.startswith(f'{SITE}/viewer.html#'))
        fragment = url.split('#', 1)[1]
        for expected in ('corpus=ws13', f'doc={"a" * 64}', 'page=2'):
            self.assertIn(expected, fragment)
        self.assertIn('s3_key=ws12%2Fresearch-copies', fragment)
        self.assertIn('viewer_key_kind=searchable', fragment)

    def test_no_s3_key_means_no_url_rather_than_a_guessed_one(self):
        # Reverting this to a default key shape makes the worksheet emit links
        # that 400 at the docs API, which reads as a broken document rather
        # than a missing lookup.
        for citation in ({}, {'document_title': 'x'}, {'s3_key': ''}):
            self.assertIsNone(worksheet.viewer_url(SITE, ITEM, citation))

    def test_the_trailing_slash_on_a_site_url_does_not_double(self):
        url = worksheet.viewer_url(SITE + '/', ITEM, CITATION)
        self.assertNotIn('//viewer.html', url.replace('https://', ''))

    def test_optional_citation_fields_are_omitted_not_empty(self):
        # An empty rights_basis= in the fragment would have the viewer print a
        # licence line with nothing in it, which reads as "no licence".
        url = worksheet.viewer_url(SITE, ITEM, {'s3_key': CITATION['s3_key']})
        self.assertNotIn('rights_basis=', url)
        self.assertNotIn('viewer_key_kind=', url)


class RenderTests(unittest.TestCase):
    def test_ocr_text_is_escaped(self):
        hostile = dict(ITEM, quote='<b>Ii\'ille<l</b> & "quoted"',
                       question='what about <script>alert(1)</script>?')
        page = worksheet.render([(hostile, CITATION, None, True, hostile['quote'])],
                                 'f.json')
        self.assertNotIn('<script>alert(1)</script>', page)
        self.assertNotIn('<b>Ii', page)
        self.assertIn('&lt;script&gt;', page)

    def test_a_row_without_a_route_says_so(self):
        page = worksheet.render([(ITEM, {}, None, None, '')], 'f.json')
        self.assertIn('no viewer route resolved', page)
        self.assertNotIn('open page', page)

    def test_the_paraphrase_rule_is_stated_on_the_page(self):
        # The one instruction a verifier cannot infer from the items.
        page = worksheet.render([(ITEM, CITATION, 'https://x/#y', True, '')],
                                 'f.json')
        self.assertIn('paraphrase', page.lower())
        self.assertIn('five of the quote', page)


class MainTests(unittest.TestCase):
    def _run(self, fixture_items, extra=()):
        # main() reports progress on stdout and stderr, which is right for an
        # operator running it and wrong inside ci/run_tests.py -- three stray
        # lines in a suite log read as a test printing debug output it forgot
        # to remove. Capture both; the assertions are about the file.
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Path(tmp) / 'fx.json'
            fixture.write_text(json.dumps({'items': fixture_items}), encoding='utf-8')
            out = Path(tmp) / 'w.html'
            with contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                code = worksheet.main([
                    '--fixture', str(fixture), '--out', str(out),
                    '--site', SITE, '--no-lookup', *extra])
            return code, (out.read_text(encoding='utf-8') if out.exists() else '')

    def test_verified_items_are_not_rendered_again(self):
        code, page = self._run([dict(ITEM, verified=True),
                                dict(ITEM, id='second', verified=False)])
        self.assertEqual(code, 0)
        self.assertIn('second', page)
        self.assertNotIn(ITEM['id'], page)

    def test_an_all_verified_fixture_writes_nothing(self):
        code, page = self._run([dict(ITEM, verified=True)])
        self.assertEqual(code, 0)
        self.assertEqual(page, '')

    def test_no_lookup_still_produces_a_usable_worksheet(self):
        # No AWS credentials must not mean no worksheet: the quote and the
        # template question are the parts a verifier cannot reconstruct.
        code, page = self._run([ITEM])
        self.assertEqual(code, 0)
        self.assertIn(ITEM['quote'], page)
        self.assertIn('no viewer route resolved', page)


class SharedRunTests(unittest.TestCase):
    """The paraphrase measure, and its agreement with the gate that enforces
    it.

    The tool carries its own copy: tests/test_ws13_known_items.py is a test
    module and importing one from a tool would make the tool's behaviour
    depend on the test suite being installed. A copy that drifts is worse than
    no copy, so the two are compared here on the cases that separate them.
    """

    CASES = (
        ('What district and county is the mine in?',
         'Mining District Name: LAVA CREEK DISTRICT County: BUTTE'),
        ('In "x.pdf", what is recorded about Payment will within fifteen '
         '(15) days from date State Land Depart-?',
         '4, Payment will be due within fifteen (15) days from the date of '
         'State Land Depart-'),
        ('How long does an operator have to settle the amount owed?',
         '4, Payment will be due within fifteen (15) days from the date of '
         'State Land Depart-'),
        ('nothing in common at all', 'wholly different words entirely'),
        ('', 'a quote with no question'),
    )

    def test_it_agrees_with_the_gate_that_enforces_it(self):
        gate = import_gate()
        for question, quote in self.CASES:
            with self.subTest(question=question[:40]):
                self.assertEqual(worksheet.shared_run(question, quote),
                                 gate.longest_shared_run(question, quote))

    def test_the_threshold_agrees_too(self):
        self.assertEqual(worksheet.MAX_SHARED_QUESTION_WORDS,
                         import_gate().MAX_SHARED_QUESTION_WORDS)

    def test_a_template_question_is_over_the_line(self):
        question, quote = self.CASES[1]
        self.assertGreater(worksheet.shared_run(question, quote),
                           worksheet.MAX_SHARED_QUESTION_WORDS)

    def test_a_paraphrase_is_under_it(self):
        question, quote = self.CASES[2]
        self.assertLessEqual(worksheet.shared_run(question, quote),
                             worksheet.MAX_SHARED_QUESTION_WORDS)


class HighlightTests(unittest.TestCase):
    def test_the_quote_is_marked_inside_the_page_context(self):
        marked = worksheet.highlight('before the QUOTE after', 'the QUOTE')
        self.assertIn('<mark>the QUOTE</mark>', marked)

    def test_context_that_does_not_carry_the_quote_is_left_alone(self):
        self.assertEqual(worksheet.highlight('abc', 'zzz'), 'abc')

    def test_ocr_cannot_close_a_tag_through_the_marker(self):
        marked = worksheet.highlight('x <script>alert(1)</script> y',
                                     '<script>')
        self.assertNotIn('<script>', marked)
        self.assertIn('&lt;script&gt;', marked)


class AcceptTests(unittest.TestCase):
    """--accept is the operator's assertion, and it still refuses the items
    the tool could already tell were wrong."""

    def fixture(self, items):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / 'items.json'
        path.write_text(json.dumps(
            {'schema_version': 1, 'target_count': 25, 'complete': False,
             'items': items}), encoding='utf-8')
        return str(path)

    def test_a_located_paraphrased_item_is_accepted(self):
        item = dict(ITEM, question='What district is this mine in?',
                    quote='Mining District Name: LAVA CREEK DISTRICT')
        path = self.fixture([item])
        accepted, refused = worksheet.accept(
            path, [(item, {}, None, True, '')])
        self.assertEqual(accepted, [item['id']])
        self.assertEqual(refused, [])
        payload = json.loads(Path(path).read_text(encoding='utf-8'))
        self.assertTrue(payload['items'][0]['verified'])
        self.assertTrue(payload['complete'])

    def test_an_unlocated_quote_is_refused(self):
        item = dict(ITEM, question='What district is this mine in?')
        path = self.fixture([item])
        accepted, refused = worksheet.accept(
            path, [(item, {}, None, False, '')])
        self.assertEqual(accepted, [])
        self.assertEqual(refused[0][0], item['id'])
        self.assertIn('not located', refused[0][1])
        payload = json.loads(Path(path).read_text(encoding='utf-8'))
        self.assertFalse(payload['items'][0]['verified'])
        self.assertFalse(payload['complete'])

    def test_an_item_whose_check_never_ran_is_refused(self):
        """--accept implies --check-quotes, so this is the shape of a caller
        that passed the wrong rows rather than of an operator decision."""
        item = dict(ITEM, question='What district is this mine in?')
        path = self.fixture([item])
        accepted, refused = worksheet.accept(
            path, [(item, {}, None, None, '')])
        self.assertEqual(accepted, [])
        self.assertEqual(len(refused), 1)

    def test_a_template_question_is_refused_even_when_located(self):
        item = dict(ITEM, question='In "x", what is recorded about Mining '
                                   'District Name LAVA CREEK DISTRICT '
                                   'County BUTTE?',
                    quote='Mining District Name: LAVA CREEK DISTRICT '
                          'County: BUTTE')
        path = self.fixture([item])
        accepted, refused = worksheet.accept(
            path, [(item, {}, None, True, '')])
        self.assertEqual(accepted, [])
        self.assertIn("quote's words in order", refused[0][1])

    def test_an_already_verified_item_is_left_alone(self):
        item = dict(ITEM, verified=True)
        path = self.fixture([item])
        accepted, refused = worksheet.accept(path, [])
        self.assertEqual((accepted, refused), ([], []))


def import_gate():
    """tests/test_ws13_known_items.py, imported as a module."""
    import importlib.util
    path = Path(__file__).resolve().parent / 'test_ws13_known_items.py'
    spec = importlib.util.spec_from_file_location('_ws13_gate', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


if __name__ == '__main__':
    unittest.main()
