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
        page = worksheet.render([(hostile, CITATION, None)], 'f.json')
        self.assertNotIn('<script>alert(1)</script>', page)
        self.assertNotIn('<b>Ii', page)
        self.assertIn('&lt;script&gt;', page)

    def test_a_row_without_a_route_says_so(self):
        page = worksheet.render([(ITEM, {}, None)], 'f.json')
        self.assertIn('no viewer route resolved', page)
        self.assertNotIn('open page', page)

    def test_the_paraphrase_rule_is_stated_on_the_page(self):
        # The one instruction a verifier cannot infer from the items.
        page = worksheet.render([(ITEM, CITATION, 'https://x/#y')], 'f.json')
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


if __name__ == '__main__':
    unittest.main()
