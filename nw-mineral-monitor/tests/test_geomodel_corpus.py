"""geomodel_corpus — the document->mine bridge and the per-mine sectionizer.

Everything here runs on synthetic manifests, grades bundles and page texts;
no PDF, no fitz, no store on disk.  The properties under test are the ones
the autopopulator's honesty depends on: the bridge never guesses, rows of one
physical mine collapse to one canonical key, and a window of text never
contains another named mine's workings.
"""
import json
import os
import shutil
import sys
import tempfile
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


# ------------------------------------------------------- the site index

def write_json(root, rel, payload):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as fh:
        json.dump(payload, fh)
    return path


def site_root():
    """A small front end on disk: two states, four namespaces, the collisions
    the resolver exists to refuse.

    Nevada carries three "Silver King"s (grades, stategeo, mrds — three
    physical mines, only the grades and mrds rows record a county), a "Twin
    Peaks" present in grades and mrds 350 m apart, a "Crown Point" in
    stategeo and mrds 44 m apart, a "Blue Jay" whose second grades row is
    unlocated, an unlocated "Far Away", and a "North Star" that Idaho also
    has.  The usmin file carries record ids (the documented hook shape) so
    the feature type reaches the index."""
    root = tempfile.mkdtemp(prefix='siteindex-')
    write_json(root, 'site/data/grades/grades.json', {
        'n': 6,
        'name': ['Blue Jay mine (bonanza shoot)', 'Blue Jay mine (mill ore)',
                 'North Star mine', 'Silver King mine', 'Far Away mine',
                 'Twin Peaks mine'],
        'st': ['NV'] * 6,
        'x': [-117.10, None, -117.30, -117.50, None, -116.00],
        'y': [38.10, None, 38.30, 38.50, None, 37.00],
        'cnty': ['Nye', 'Nye', 'Esmeralda', 'Nye', None, 'Lincoln'],
        'dist': [None] * 6,
        'quote': ['q%d' % i for i in range(6)],
        'au': [None] * 6, 'ag': [None] * 6, 'pb': [None] * 6,
    })
    write_json(root, 'build-inputs/data/sites/stategeo_nv.json', {
        'src': 'stategeo', 'state': 'NV', 'n': 3,
        'id': ['NBMG SK0001', 'NBMG CP0002', 'NBMG BJ0077'],
        'nm': ['Silver King Mine', 'Crown Point Mine', 'Blue Jay Mine, Jay Bird'],
        'ty': ['LODE', 'LODE', 'LODE'],
        'x': [-118.0, -117.2, -117.101], 'y': [39.0, 38.2, 38.101],
    })
    write_json(root, 'build-inputs/data/sites/mrds_nv.json', {
        'src': 'mrds', 'state': 'NV', 'n': 3,
        'id': ['10000001', '10000002', '10000003'],
        'nm': ['Silver King', 'Twin Peaks Mine', 'Crown Point'],
        'st': ['PR', 'PR', 'PR'], 'g': [0, 0, 0], 'c': ['Gold', 'Silver', 'Gold'],
        'county': ['Lander', 'Lincoln', 'Nye'],
        'x': [-117.9, -116.003, -117.2005], 'y': [39.1, 37.002, 38.2],
    })
    write_json(root, 'build-inputs/data/sites/usmin_nv.json', {
        'src': 'usmin', 'state': 'NV', 'n': 2, 'types': ['Adit', 'Mine Shaft'],
        'id': ['501', '502'], 'nm': ['Crown Point Adit', None], 't': [0, 1],
        'x': [-117.2, -117.21], 'y': [38.2, 38.21],
    })
    write_json(root, 'build-inputs/data/sites/stategeo_id.json', {
        'src': 'stategeo', 'state': 'ID', 'n': 1,
        'id': ['IGS DD-1 NS0001'], 'nm': ['North Star Mine'],
        'x': [-114.0], 'y': [44.0],
    })
    return root


def row(front_end_id, method='embedded_code', relation='identity', confidence=1.0,
        verified=True):
    return {'front_end_id': front_end_id, 'method': method, 'relation': relation,
            'confidence': confidence, 'verified': verified}


class SiteIndexTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.root = site_root()
        cls.index = corpus.SiteIndex(cls.root)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, ignore_errors=True)

    def doc(self, **kw):
        base = {'sha256': 'ab' * 32, 'state': 'NV', 'county': None, 'mine_ids': [],
                'mine_names': [], 'title': '', 'portal': 'test', 'doc_type': 'mine file',
                'front_end_ids': []}
        base.update(kw)
        return base

    # -- tier 1
    def test_tier1_verified_identity_row_wins_in_every_spelling(self):
        for fid in ('stategeo:NBMG CP0002', 'stategeo-nbmg-cp0002', 'NBMG CP0002'):
            got = self.index.resolve(self.doc(front_end_ids=[row(fid)]))
            self.assertEqual(got['status'], 'resolved', fid)
            cand = got['candidates'][0]
            self.assertEqual(cand['mine_key'], 'stategeo:NBMG CP0002')
            self.assertEqual(cand['method'], 'identity_row')
            self.assertEqual(got['tiers_tried'][0]['tier'], 1)
            # the same hole in MRDS rides along under 'also', with its distance
            self.assertEqual([a['mine_key'] for a in cand['also']], ['mrds:10000003'])
            self.assertLess(cand['also'][0]['distance_km'], 0.1)
            self.assertIn(fid, cand['evidence']['summary'])

    def test_unverified_identity_row_is_reported_not_used(self):
        got = self.index.resolve(self.doc(
            front_end_ids=[row('stategeo:NBMG CP0002', method='fuzzy_name',
                               confidence=0.55, verified=False)]))
        self.assertEqual(got['status'], 'parked')
        self.assertIn('not verified', got['tiers_tried'][0]['notes'][0])
        self.assertEqual(got['candidates'], [])

    def test_an_unlocated_row_is_reported_never_built(self):
        got = self.index.resolve(self.doc(front_end_ids=[row('grades:4')]))
        self.assertEqual(got['status'], 'parked')
        self.assertIn('unlocated', got['tiers_tried'][0]['notes'][0])

    def test_an_alias_grades_row_reaches_its_canonical(self):
        site = self.index.get('grades:1')
        self.assertEqual(site['mine_key'], 'grades:0')
        self.assertEqual(site['rows'], [0, 1])
        self.assertIn('blue jay', site['keys'])

    # -- tier 2
    def test_tier2_embedded_code_resolves_a_survey_code(self):
        got = self.index.resolve(self.doc(mine_ids=['cp0002', 'ADMM-1552428849304-690']))
        self.assertEqual(got['status'], 'resolved')
        cand = got['candidates'][0]
        self.assertEqual(cand['mine_key'], 'stategeo:NBMG CP0002')
        self.assertEqual(cand['method'], 'embedded_code')
        self.assertEqual([a['mine_key'] for a in cand['also']], ['mrds:10000003'])
        self.assertIn("code cp0002 = site id 'NBMG CP0002'", cand['evidence']['summary'])
        self.assertTrue(any('ADMM' in n for n in cand['evidence']['notes']))

    def test_a_digit_only_code_is_never_probed(self):
        got = self.index.resolve(self.doc(mine_ids=['10000003']))
        self.assertEqual(got['status'], 'parked')
        self.assertIn('digit-only', got['tiers_tried'][1]['notes'][0])

    # -- tiers 3 and 4
    def test_tier3_county_disambiguates_a_shared_name(self):
        got = self.index.resolve(self.doc(mine_names=['Silver King'], county='Nye County'))
        self.assertEqual(got['status'], 'resolved')
        cand = got['candidates'][0]
        self.assertEqual(cand['mine_key'], 'grades:3')
        self.assertEqual(cand['method'], 'name_state_county')
        self.assertEqual(cand['also'], [])
        ev = cand['evidence']
        self.assertEqual(ev['county'], 'nye')
        self.assertEqual(ev['normalised'], ['silver king'])
        self.assertEqual(ev['county_unrecorded'], ['stategeo:NBMG SK0001'])
        self.assertIn("'nye' = 'Nye'", ev['summary'])

    def test_tier4_shared_name_without_county_parks_with_every_candidate(self):
        got = self.index.resolve(self.doc(mine_names=['Silver King']))
        self.assertEqual(got['status'], 'parked')
        self.assertTrue(got['reason'].startswith('ambiguous'), got['reason'])
        self.assertEqual({c['mine_key'] for c in got['candidates']},
                         {'grades:3', 'stategeo:NBMG SK0001', 'mrds:10000001'})
        tier3 = next(t for t in got['tiers_tried'] if t['tier'] == 3)
        self.assertIn('no county', tier3['outcome'])

    def test_merge_by_distance_prefers_grades_and_records_the_distance(self):
        got = self.index.resolve(self.doc(mine_names=['Twin Peaks']))
        self.assertEqual(got['status'], 'resolved')
        cand = got['candidates'][0]
        self.assertEqual(cand['mine_key'], 'grades:5')
        self.assertEqual(cand['method'], 'name_state')
        self.assertEqual(len(cand['also']), 1)
        self.assertEqual(cand['also'][0]['mine_key'], 'mrds:10000002')
        self.assertLess(cand['also'][0]['distance_km'], 0.5)
        self.assertEqual(cand['evidence']['merged'], cand['also'])
        self.assertIn('km', cand['evidence']['summary'])

    def test_merge_prefers_the_survey_record_over_mrds(self):
        got = self.index.resolve(self.doc(mine_names=['Crown Point mine']))
        cand = got['candidates'][0]
        self.assertEqual(got['status'], 'resolved')
        self.assertEqual(cand['mine_key'], 'stategeo:NBMG CP0002')
        self.assertEqual([a['mine_key'] for a in cand['also']], ['mrds:10000003'])
        # a workings symbol with a different core name is not the mine
        self.assertNotIn('usmin:501', [a['mine_key'] for a in cand['also']])

    def test_evidence_says_exactly_what_matched(self):
        cand = self.index.resolve(self.doc(mine_names=['Twin Peaks']))['candidates'][0]
        ev = cand['evidence']
        self.assertEqual(ev['names'], ['Twin Peaks'])
        self.assertEqual(ev['state'], 'NV')
        self.assertEqual(ev['matched'][0]['alias'], 'Twin Peaks mine')
        self.assertEqual(ev['matched'][0]['normalised'], 'twin peaks')
        self.assertIn("'Twin Peaks' -> 'twin peaks' = 'Twin Peaks mine' (grades:5)", ev['summary'])

    def test_state_blocks_and_a_missing_state_is_reported(self):
        nv = self.index.resolve(self.doc(mine_names=['North Star']))
        self.assertEqual(nv['candidates'][0]['mine_key'], 'grades:2')
        idaho = self.index.resolve(self.doc(mine_names=['North Star'], state='Idaho'))
        self.assertEqual(idaho['status'], 'resolved')
        self.assertEqual(idaho['candidates'][0]['mine_key'], 'stategeo:IGS DD-1 NS0001')
        none = self.index.resolve(self.doc(mine_names=['North Star'], state=None))
        self.assertEqual(none['status'], 'parked')
        self.assertEqual({c['state'] for c in none['candidates']}, {'NV', 'ID'})
        self.assertIn('none on the document', none['candidates'][0]['evidence']['state'])

    def test_fuzzy_is_reported_never_resolved(self):
        got = self.index.resolve(self.doc(mine_names=['Silver Kings'], county='Nye'))
        self.assertEqual(got['status'], 'parked')
        self.assertEqual(got['candidates'], [])
        fuzzy = next(t for t in got['tiers_tried'] if t['tier'] == 'fuzzy')
        self.assertEqual(fuzzy['near'][0]['near'], 'silver king')
        self.assertIn('never resolved', fuzzy['outcome'])
        self.assertIn('near-misses', got['reason'])

    def test_a_document_naming_two_mines_is_parked_and_carved_per_mine(self):
        doc = self.doc(mine_names=['Crown Point', 'Twin Peaks'])
        got = self.index.resolve(doc)
        self.assertEqual(got['status'], 'parked')
        self.assertTrue(got['reason'].startswith('several mines named'), got['reason'])
        self.assertEqual({c['mine_key'] for c in got['candidates']},
                         {'stategeo:NBMG CP0002', 'grades:5'})
        carve = corpus.targets_for(doc, '', index=self.index)
        self.assertEqual({t['mine_key'] for t in carve['targets']},
                         {'stategeo:NBMG CP0002', 'grades:5'})

    # -- tier 5
    def test_district_row_parks_as_container_and_names_resolve_per_mine(self):
        got = self.index.resolve(self.doc(
            mine_names=['Candelaria'],
            front_end_ids=[row('mrds:10000003', method='district_name', relation='district')]))
        self.assertEqual(got['status'], 'parked')
        self.assertTrue(got['reason'].startswith('district/county container'), got['reason'])
        self.assertEqual(got['candidates'], [])
        self.assertTrue(all('place names' in t['outcome']
                            for t in got['tiers_tried'] if t['tier'] in (3, 4)))
        named = self.index.resolve_names(['Crown Point', 'Silver King', 'Nowhere'], 'NV')
        self.assertEqual(named['Crown Point']['status'], 'resolved')
        self.assertEqual(named['Crown Point']['candidates'][0]['mine_key'], 'stategeo:NBMG CP0002')
        self.assertEqual(named['Silver King']['status'], 'ambiguous')
        self.assertEqual(len(named['Silver King']['candidates']), 3)
        self.assertEqual(named['Nowhere']['status'], 'unmatched')

    def test_a_title_that_reads_the_name_as_a_district_parks(self):
        got = self.index.resolve(self.doc(
            mine_names=['Crown Point'], title='Geology of the Crown Point mining district'))
        self.assertEqual(got['status'], 'parked')
        self.assertIn('district/county container', got['reason'])
        self.assertIn('title', got['reason'])

    # -- targets
    def test_targets_for_a_district_report_carves_one_section_per_mine(self):
        doc = self.doc(front_end_ids=[row('mrds:10000003', relation='district')])
        text = ('The Crown Point mine is opened by an adit driven N45E for 500 feet. '
                'The Twin Peaks mine has a shaft 300 feet deep. The Silver King mine adjoins.')
        carve = corpus.targets_for(doc, [text], index=self.index)
        self.assertEqual(carve['resolution']['status'], 'parked')
        keys = {t['mine_key']: t for t in carve['targets']}
        self.assertEqual(set(keys), {'stategeo:NBMG CP0002', 'grades:5'})
        crown = keys['stategeo:NBMG CP0002']
        self.assertIn('Crown Point', crown['cores'])
        self.assertIn('Twin Peaks', crown['other_cores'])
        self.assertIn('Silver King', crown['other_cores'])
        self.assertNotIn('Crown Point', crown['other_cores'])
        self.assertEqual(carve['named']['Silver King']['status'], 'ambiguous')
        # the ambiguous name is a cut for everyone and a target for no one
        self.assertIn('Silver King', carve['other_cores'])

    def test_targets_for_a_resolved_document_is_one_target_with_the_rest_as_cuts(self):
        doc = self.doc(mine_names=['Twin Peaks'])
        text = 'The Twin Peaks shaft is 300 feet deep. The Crown Point mine adjoins it.'
        carve = corpus.targets_for(doc, text, index=self.index)
        self.assertEqual(len(carve['targets']), 1)
        target = carve['targets'][0]
        self.assertEqual(target['mine_key'], 'grades:5')
        self.assertEqual(target['via'], 'resolved')
        self.assertIn('Twin Peaks', target['cores'])
        self.assertIn('Crown Point', target['other_cores'])
        self.assertEqual(carve['named'], {})

    # -- the index itself
    def test_usmin_site_carries_its_feature_type(self):
        adit = self.index.get('usmin:501', 'NV')
        self.assertEqual(adit['type'], 'Adit')
        self.assertTrue(adit['located'])
        self.assertEqual(self.index.get('usmin:502', 'NV')['type'], 'Mine Shaft')

    def test_ardf_reports_no_source_on_this_machine(self):
        cover = self.index.coverage('AK')
        self.assertEqual(cover['ardf'], corpus.NO_SOURCE)
        self.assertEqual(corpus.NO_SOURCE, 'no source on this machine')
        got = self.index.resolve(self.doc(state='AK', mine_names=['Miners River']))
        self.assertEqual(got['status'], 'parked')

    def test_ardf_loads_from_the_alaska_crosswalk_when_present(self):
        root = tempfile.mkdtemp(prefix='siteindex-ak-')
        try:
            write_json(root, 'grades-research/ak/ardf_target_crosswalk.json', {
                'records': [{'ardf_no': 'AN144', 'site': 'Miners River; Miners River Nickel',
                             'latitude': 61.09, 'longitude': -147.41,
                             'district': 'Prince William Sound', 'commodities_main': 'Co, Cu, Ni'}]})
            idx = corpus.SiteIndex(root)
            self.assertIn('partial', idx.coverage('AK')['ardf'])
            site = idx.get('ardf:AN144')
            self.assertEqual(site['keys'], ['miners river', 'miners river nickel'])
            got = idx.resolve(self.doc(state='AK', mine_names=['Miners River Nickel']))
            self.assertEqual(got['status'], 'resolved')
            self.assertEqual(got['candidates'][0]['mine_key'], 'ardf:AN144')
            self.assertEqual(got['candidates'][0]['kind'], 'ardf')
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_loading_is_lazy_per_state(self):
        idx = corpus.SiteIndex(self.root)
        self.assertEqual(idx.loaded_states(), [])
        idx.resolve(self.doc(mine_names=['Twin Peaks']))
        self.assertEqual(idx.loaded_states(), ['NV'])
        self.assertEqual(sorted(idx.coverage('NV')), ['ardf', 'grades', 'mrds', 'stategeo', 'usmin'])
        self.assertIn('Alaska-only', idx.coverage('NV')['ardf'])

    def test_load_is_cached_per_root(self):
        self.assertIs(corpus.SiteIndex.load(self.root), corpus.SiteIndex.load(self.root))

    def test_nothing_to_resolve_says_so(self):
        got = self.index.resolve(self.doc())
        self.assertEqual(got['status'], 'parked')
        self.assertIn('names no mine', got['reason'])


REAL_SITES = ROOT / 'build-inputs' / 'data' / 'sites'
REAL_GRADES = ROOT / 'site' / 'data' / 'grades' / 'grades.json'


@unittest.skipUnless((REAL_SITES / 'stategeo_id.json').exists() and
                     (REAL_SITES / 'mrds_id.json').exists() and REAL_GRADES.exists(),
                     'build-inputs/data/sites (stategeo_id, mrds_id) and the grades bundle '
                     'are not on this machine')
class RealSiteIndexTests(unittest.TestCase):
    """The shipped site files are what the WS13 driver will resolve against."""

    @classmethod
    def setUpClass(cls):
        cls.index = corpus.SiteIndex.load()

    def test_idaho_st_louis_resolves_by_its_code_and_merges_three_namespaces(self):
        got = self.index.resolve({
            'sha256': 'cd' * 32, 'state': 'ID', 'county': 'Butte', 'mine_ids': ['IF0126'],
            'mine_names': ['St. Louis Mine'], 'title': 'IF0126', 'portal': 'igs',
            'doc_type': 'mine file', 'front_end_ids': []})
        self.assertEqual(got['status'], 'resolved', got['reason'])
        cand = got['candidates'][0]
        self.assertEqual(cand['method'], 'embedded_code')
        self.assertEqual(cand['kind'], 'grades')
        also = {a['mine_key']: a['distance_km'] for a in cand['also']}
        self.assertIn('stategeo:IGS DD-1 IF0126', also)
        self.assertTrue(any(k.startswith('mrds:') for k in also), also)
        self.assertTrue(all(d <= corpus.SAME_MINE_KM for d in also.values()), also)
        self.assertEqual(cand['state'], 'ID')

    def test_idaho_north_star_is_ambiguous_without_a_county_and_settled_with_one(self):
        base = {'sha256': 'ef' * 32, 'state': 'ID', 'county': None, 'mine_ids': [],
                'mine_names': ['North Star'], 'title': '', 'portal': 'igs',
                'doc_type': 'mine file', 'front_end_ids': []}
        got = self.index.resolve(base)
        self.assertEqual(got['status'], 'parked')
        self.assertGreater(len(got['candidates']), 1)
        with_county = self.index.resolve(dict(base, county='Blaine'))
        self.assertEqual(with_county['status'], 'resolved', with_county['reason'])
        cand = with_county['candidates'][0]
        self.assertEqual(cand['kind'], 'grades')
        self.assertEqual(cand['county'], 'Blaine')
        self.assertEqual(cand['method'], 'name_state_county')

    def test_coverage_reports_usmin_without_record_ids(self):
        cover = self.index.coverage('ID')
        self.assertTrue(cover['grades'].startswith('loaded'))
        self.assertTrue(cover['stategeo'].startswith('loaded'))
        self.assertTrue(cover['mrds'].startswith('loaded'))
        self.assertIn('no record ids', cover['usmin'])

    def test_the_alaska_crosswalk_feeds_ardf_when_present(self):
        rel = os.path.join('grades-research', 'ak', 'ardf_target_crosswalk.json')
        if not (ROOT / rel).exists():
            self.skipTest('%s is not on this machine' % rel)
        cover = self.index.coverage('AK')
        self.assertTrue(cover['ardf'].startswith('loaded'), cover['ardf'])
        self.assertIn('partial', cover['ardf'])


if __name__ == '__main__':
    unittest.main()
