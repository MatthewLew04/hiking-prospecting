"""geomodel_corpus — the document->mine bridge and the per-mine sectionizer.

Everything here runs on synthetic manifests, grades bundles and page texts;
no PDF, no fitz, no store on disk.  The properties under test are the ones
the autopopulator's honesty depends on: the bridge never guesses, rows of one
physical mine collapse to one canonical key, and a window of text never
contains another named mine's workings.
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'pipelines'))

import geomodel_corpus as corpus  # noqa: E402


def grades_bundle():
    """Six rows, two of them the same physical mine, one unlocated."""
    return {
        'n': 6,
        'name': ['Blue Jay mine (bonanza shoot)', 'Blue Jay mine (mill ore)',
                 'Crown Point mine', 'Lone Star mine', 'Far Away mine',
                 'Crown Point mine'],
        'st': ['NV', 'NV', 'NV', 'UT', 'NV', 'UT'],
        'x': [-117.1, None, -117.3, -113.2, None, -113.4],
        'y': [38.1, None, 38.3, 40.2, None, 40.4],
        'quote': ['The ore averaged 25 ounces of silver to the ton.',
                  'Mill heads ran $8 a ton.',
                  'Picked samples assayed 4 ounces of gold.',
                  'Shipments averaged 12 per cent lead.',
                  'A selected sample carried 60 ounces of silver.',
                  'The dump ore ran 2 ounces of silver.'],
        'au': [None, None, 4.0, None, None, None],
        'ag': [25.0, 8.0, None, None, 60.0, 2.0],
        'pb': [None, None, None, 12.0, None, None],
    }


def manifest():
    return {
        'documents': [{
            'doc_id': 'd1' * 32,
            'title': 'Ore Deposits of the Example District',
            'mine_id': 'district-example',
            'source_url': 'https://example.test/report.pdf',
            'publication_year': 1921, 'pages': 3,
            'subjects': [
                {'label': 'Ore Deposits of the Example District',
                 'mine_id': 'district-example', 'state': 'NV'},
                {'label': 'Blue Jay mine', 'mine_id': 'ws9-nv-blue-jay', 'state': 'NV'},
                {'label': 'Crown Point mine', 'mine_id': 'ws9-nv-crown-point', 'state': 'NV'},
                {'label': 'Far Away mine', 'mine_id': 'ws9-nv-far-away', 'state': 'NV'},
                {'label': 'Mystery mine', 'mine_id': 'ws9-nv-mystery', 'state': 'NV'},
            ],
        }],
        'citations': [
            {'citation_id': 'c1', 'doc_id': 'd1' * 32,
             'mine_id': 'district-example', 'mine_name': 'Blue Jay mine',
             'page': 2, 'quote': 'The ore averaged 25 ounces of silver to the ton.',
             'quote_located': True, 'state': 'NV'},
            {'citation_id': 'c2', 'doc_id': 'd1' * 32,
             'mine_id': 'district-example', 'mine_name': 'Lone Star mine',
             'page': 3, 'quote': 'Shipments averaged 12 per cent lead.',
             'quote_located': False, 'state': 'UT'},
        ],
    }


def evidence():
    return {
        'ws9-nv-blue-jay': {'name': 'Blue Jay mine', 'state': 'NV',
                            'district': 'Example', 'county': None},
        'ws9-nv-crown-point': {'name': 'Crown Point mine', 'state': 'NV',
                               'district': 'Example', 'county': None},
        'ws9-nv-far-away': {'name': 'Far Away mine', 'state': 'NV',
                            'district': 'Example', 'county': None},
        'ws9-nv-mystery': {'name': 'Mystery mine', 'state': 'NV',
                           'district': 'Example', 'county': None},
    }


def bridges(**kw):
    args = dict(grades=grades_bundle(), evidence=evidence(), stategeo={})
    args.update(kw)
    return corpus.build_bridges(manifest(), **args)


class CoreLabelTests(unittest.TestCase):

    def test_suffixes_and_parentheticals_strip(self):
        self.assertEqual(corpus.core_label('Bullion mine (Gulch/Eureka)'), 'Bullion')
        self.assertEqual(corpus.core_label('Hard Scrabble group'), 'Hard Scrabble')
        self.assertEqual(corpus.core_label('Sec. 12 arsenopyrite-quartz test-pit prospect'),
                         'Sec. 12 arsenopyrite-quartz test-pit')

    def test_stacked_suffixes_strip_to_the_name(self):
        self.assertEqual(corpus.core_label('Julia Dean mine'), 'Julia Dean')


class GradeGroupTests(unittest.TestCase):

    def test_same_name_and_state_is_one_group_with_a_located_canonical(self):
        groups, canon = corpus._grade_groups(grades_bundle())
        self.assertEqual(canon[0], 0)
        self.assertEqual(canon[1], 0)   # unlocated row folds into the located one

    def test_same_name_in_another_state_is_a_different_mine(self):
        _, canon = corpus._grade_groups(grades_bundle())
        self.assertEqual(canon[2], 2)   # Crown Point NV
        self.assertEqual(canon[5], 5)   # Crown Point UT

    def test_a_group_with_no_located_row_has_no_canonical(self):
        _, canon = corpus._grade_groups(grades_bundle())
        self.assertIsNone(canon[4])     # Far Away: x/y null

    def test_same_name_far_apart_never_collapses(self):
        g = grades_bundle()
        # two located "Blue Jay mine" rows in NV, 4 degrees apart
        g['x'][1] = -113.1
        g['y'][1] = 41.9
        _, canon = corpus._grade_groups(g)
        self.assertEqual(canon[0], 0)
        self.assertEqual(canon[1], 1)   # its own mine, not an alias

    def test_different_districts_are_different_mines(self):
        g = grades_bundle()
        g['dist'] = ['Aurum', 'Cherry Creek', None, None, None, None]
        g['x'][1] = g['x'][0]
        g['y'][1] = g['y'][0]
        _, canon = corpus._grade_groups(g)
        self.assertNotEqual(canon[0], canon[1])

    def test_a_district_row_never_welds_to_a_mine_row(self):
        # resolve.normalise would equate "Tintic district" and "Tintic mine";
        # the mild grouping key must not
        g = {'n': 2, 'name': ['Tintic district (average)', 'Tintic mine'],
             'st': ['UT', 'UT'], 'x': [-112.1, -112.1], 'y': [39.9, 39.9],
             'quote': ['a', 'b'], 'au': [None, None], 'ag': [None, None],
             'pb': [None, None]}
        _, canon = corpus._grade_groups(g)
        self.assertNotEqual(canon[0], canon[1])


class BridgeTests(unittest.TestCase):

    def test_citation_quote_join_is_per_citation_not_per_document(self):
        got = bridges()
        by_cit = {c['citation_id']: c for c in got['citation_links']}
        # both citations carry the container id district-example, yet each
        # resolves to its own mine
        self.assertEqual(by_cit['c1']['link']['mine_id'], 'grades:0')
        self.assertEqual(by_cit['c2']['link']['mine_id'], 'grades:3')

    def test_a_quote_collision_with_another_mines_row_is_parked(self):
        # identical boilerplate under a different mine name must not weld the
        # citation to that row
        m = manifest()
        m['citations'].append(
            {'citation_id': 'c4', 'doc_id': 'd1' * 32,
             'mine_id': 'district-example', 'mine_name': 'Totally Other mine',
             'page': 3, 'quote': 'Picked samples assayed 4 ounces of gold.',
             'quote_located': True, 'state': 'NV'})
        got = corpus.build_bridges(m, grades=grades_bundle(),
                                   evidence=evidence(), stategeo={})
        self.assertNotIn('c4', {c['citation_id'] for c in got['citation_links']})
        parked = {p.get('citation_id'): p['reason'] for p in got['parked']}
        self.assertEqual(parked.get('c4'), 'quote-name-mismatch')

    def test_a_quote_join_across_states_is_parked(self):
        m = manifest()
        m['citations'].append(
            {'citation_id': 'c5', 'doc_id': 'd1' * 32,
             'mine_id': 'district-example', 'mine_name': 'Crown Point mine',
             'page': 3, 'quote': 'Picked samples assayed 4 ounces of gold.',
             'quote_located': True, 'state': 'UT'})   # the row is NV
        got = corpus.build_bridges(m, grades=grades_bundle(),
                                   evidence=evidence(), stategeo={})
        parked = {p.get('citation_id'): p['reason'] for p in got['parked']}
        self.assertEqual(parked.get('c5'), 'quote-state-mismatch')

    def test_unlocated_quote_join_is_parked_not_guessed(self):
        m = manifest()
        m['citations'].append(
            {'citation_id': 'c3', 'doc_id': 'd1' * 32,
             'mine_id': 'district-example', 'mine_name': 'Far Away mine',
             'page': 3, 'quote': 'A selected sample carried 60 ounces of silver.',
             'quote_located': True, 'state': 'NV'})
        got = corpus.build_bridges(m, grades=grades_bundle(),
                                   evidence=evidence(), stategeo={})
        self.assertNotIn('c3', {c['citation_id'] for c in got['citation_links']})
        self.assertIn('unlocated', {p['reason'] for p in got['parked']})

    def test_evidence_name_reaches_the_canonical_row(self):
        got = bridges()
        link = got['links']['ws9-nv-blue-jay']
        self.assertEqual(link['mine_id'], 'grades:0')
        self.assertEqual(link['method'], 'evidence_name')
        self.assertEqual(link['rows'], [0, 1])

    def test_a_name_the_bundle_lacks_is_parked_with_a_reason(self):
        got = bridges()
        parked = {p['store_mine_id']: p['reason'] for p in got['parked']
                  if 'store_mine_id' in p}
        self.assertEqual(parked.get('ws9-nv-mystery'), 'no-buildable-reference')
        self.assertEqual(parked.get('district-example'), 'district-not-a-mine')

    def test_stategeo_subject_links_by_site_record(self):
        m = manifest()
        m['documents'][0]['subjects'].append(
            {'label': 'St. Louis Mine', 'mine_id': 'stategeo-igs-dd-1-if0126',
             'state': 'ID'})
        sites = {'stategeo-igs-dd-1-if0126': {
            'front_end_id': 'stategeo:IGS DD-1 IF0126', 'name': 'St. Louis Mine',
            'lon': -113.6, 'lat': 43.6, 'state': 'ID'}}
        got = corpus.build_bridges(m, grades=grades_bundle(),
                                   evidence=evidence(), stategeo=sites)
        link = got['links']['stategeo-igs-dd-1-if0126']
        self.assertEqual(link['kind'], 'latlon')
        self.assertEqual(link['front_end_id'], 'stategeo:IGS DD-1 IF0126')

    def test_groups_expose_every_row_of_a_canonical(self):
        got = bridges()
        self.assertEqual(got['groups']['grades:0'], [0, 1])


class NamedMineTests(unittest.TestCase):

    def test_multi_word_names_cut_on_one_sighting(self):
        text = 'The vein continues toward the Good Hope mine on the east.'
        self.assertEqual(corpus._named_mines(text, {'Blue Jay'}), {'Good Hope'})

    def test_single_word_names_must_recur(self):
        once = 'Considerable stoping was done above the Drake shaft here.'
        self.assertEqual(corpus._named_mines(once, {'Blue Jay'}), set())
        twice = once + ' Later the Drake shaft was deepened.'
        self.assertEqual(corpus._named_mines(twice, {'Blue Jay'}), {'Drake'})

    def test_sentence_starters_and_articles_are_not_names(self):
        text = ('Another shaft was sunk. Another shaft was started. '
                'The Accessible workings were mapped. See Good Hope mine. '
                'Other claims were located. Other claims lapsed.')
        got = corpus._named_mines(text, {'Blue Jay'})
        self.assertEqual(got, {'Good Hope'})

    def test_the_target_itself_is_never_a_cutoff(self):
        text = 'The Blue Jay mine was reopened. The Blue Jay mine paid well.'
        got = corpus._named_mines(text, {'Blue Jay'})
        self.assertEqual(got, set())

    def test_an_extension_of_the_target_is_a_different_mine_and_cuts(self):
        # "Blue Jay Extension" contains the target but is another hole in the
        # ground; the longest-match scan keeps the overlap from cutting the
        # target's own name
        text = 'The Blue Jay mine adjoins the Blue Jay Extension claim.'
        got = corpus._named_mines(text, {'Blue Jay'})
        self.assertEqual(got, {'Blue Jay Extension'})

    def test_no_match_across_a_line_break(self):
        text = 'BOSTON\nThe Boston mine is idle. The Boston mine flooded.'
        got = corpus._named_mines(text, {'Blue Jay'})
        self.assertEqual(got, {'Boston'})


class SectionTests(unittest.TestCase):
    PAGES = [
        'CONTENTS\nBlue Jay mine ____________ 2\nCrown Point mine _________ 3\n',
        'BLUE JAY MINE\nThe Blue Jay mine is developed by a shaft 300 feet '
        'deep and a crosscut driven 120 feet to the vein. The ore carries '
        'silver.\n',
        'CROWN POINT MINE\nThe Crown Point mine is opened by an adit driven '
        'N45E for 500 feet.\n',
    ]

    def test_window_ends_where_the_next_mine_begins(self):
        got = corpus.sections(self.PAGES, {'Blue Jay'}, {'Crown Point'})
        self.assertTrue(got)
        joined = '\n'.join(s['text'] for s in got)
        self.assertIn('shaft 300 feet', joined)
        self.assertNotIn('adit driven', joined)

    def test_toc_lines_alone_are_not_a_section(self):
        got = corpus.sections([self.PAGES[0]], {'Blue Jay'}, {'Crown Point'})
        self.assertEqual(got, [])

    def test_pages_are_one_based_pdf_pages(self):
        got = corpus.sections(self.PAGES, {'Blue Jay'}, {'Crown Point'})
        self.assertIn(2, got[0]['pages'])

    def test_a_located_quote_anchors_where_the_name_is_absent(self):
        pages = ['The property is developed by two adits driven along the '
                 'vein for 800 feet. The ore averaged 25 ounces of silver '
                 'to the ton.\n']
        got = corpus.sections(pages, {'Nameless'}, set(),
                              quotes=['The ore averaged 25 ounces of silver to the ton.'])
        self.assertTrue(got)
        self.assertIn('two adits', got[0]['text'])

    def test_other_mines_workings_never_leak_into_the_window(self):
        pages = ['The Blue Jay shaft is 300 feet deep. At the Good Hope mine '
                 'a winze was sunk 200 feet. At the Good Hope mine work '
                 'stopped.\n']
        got = corpus.sections(pages, {'Blue Jay'}, set())
        joined = '\n'.join(s['text'] for s in got)
        self.assertIn('300 feet', joined)
        self.assertNotIn('winze', joined)

    def test_a_target_inside_a_longer_name_never_anchors(self):
        # "King" must not claim the Silver King's shaft
        pages = ['The Silver King mine is developed by a shaft 300 feet deep '
                 'and two levels.\n']
        got = corpus.sections(pages, {'King'}, {'Silver King'})
        self.assertEqual(got, [])

    def test_a_cut_inside_the_targets_own_name_never_truncates(self):
        # the reverse: "Silver King" must keep its section although "King"
        # is a registered other subject
        pages = ['The Silver King mine is developed by a shaft 300 feet deep '
                 'and two levels. The King mine adjoins it on the north.\n']
        got = corpus.sections(pages, {'Silver King'}, {'King'})
        joined = '\n'.join(s['text'] for s in got)
        self.assertIn('shaft 300 feet', joined)
        self.assertNotIn('adjoins', joined)


class AssignmentTests(unittest.TestCase):

    def _assignments(self):
        m = manifest()
        pages = SectionTests.PAGES

        def fake_page_texts(doc, store_root=None, cache_dir=None):
            return pages
        original = corpus.page_texts
        corpus.page_texts = fake_page_texts
        try:
            return corpus.assignments(
                manifest=m,
                bridges=corpus.build_bridges(m, grades=grades_bundle(),
                                             evidence=evidence(), stategeo={}),
                log=lambda *a: None)
        finally:
            corpus.page_texts = original

    def test_units_key_by_canonical_mine_and_merge_tiers(self):
        got = self._assignments()
        units = {u['key']: u for u in got['units']}
        blue = units['grades:0']
        self.assertIn('citation_quote', blue['methods'])
        self.assertIn('evidence_name', blue['methods'])
        self.assertEqual(blue['grade_rows'], [0, 1])
        self.assertTrue(blue['texts'])

    def test_a_bridged_mine_with_no_descriptive_text_still_lists_documents(self):
        got = self._assignments()
        units = {u['key']: u for u in got['units']}
        lone = units['grades:3']   # cited on a page its name never appears on
        self.assertEqual(len(lone['documents']), 1)

    def test_no_section_text_is_duplicated_between_tiers(self):
        got = self._assignments()
        units = {u['key']: u for u in got['units']}
        spans = [(t['doc_id'], tuple(t['span'])) for t in units['grades:0']['texts']]
        self.assertEqual(len(spans), len(set(spans)))


if __name__ == '__main__':
    unittest.main()
