"""geomodel.composition — the minerals a description names, read off the
lexicon, tied to the level the sentence names, placed on the workings the
builder placed, and never inferred past the words."""
import json
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'pipelines'))
sys.path.insert(0, str(ROOT / 'services' / 'minevis'))

from geomodel import agentbuild, assay, composition, narrative, publish, render2d  # noqa: E402
from geomodel.narrative import FT  # noqa: E402

SITE = {'name': 'Silver King', 'mine_id': 'grades:17', 'lon': -116.87, 'lat': 36.877,
        'elevation_m': 1900.0, 'source': 'USGS Bulletin 723',
        'source_url': 'https://example.invalid/b723'}

TEXT = ('On the 300 level the vein carries galena, sphalerite and a little chalcopyrite in a '
        'gangue of quartz and calcite; above the 100 level the ore is oxidized, with cerussite '
        'and limonite. The Main shaft is 620 feet deep. A crosscut on the 300 level cuts the '
        'vein 90 feet from the shaft.')

#: the same mine, with the shaft's own sentence naming minerals but no level.
#: (A separate "The Main shaft cut pyrite" sentence would be a *mention* to
#: narrative — it describes no geometry — and a mention names no working.)
TEXT_COLLAR = TEXT.replace('The Main shaft is 620 feet deep.',
                           'The Main shaft is 620 feet deep and cut pyrite and quartz in the '
                           'sulphide zone.')


def read(text=TEXT):
    return composition.compose(text, narrative.parse(text))


def by_name(comp, name):
    return [m for m in comp['minerals'] if m['name'] == name]


class LexiconTests(unittest.TestCase):
    def test_the_lexicon_is_the_size_the_task_asked_for_and_says_what_each_entry_is(self):
        self.assertGreaterEqual(len(composition.MINERALS), 120)
        for name, role, commodities, surfaces in composition.MINERALS:
            self.assertIn(role, ('ore', 'gangue', 'alteration', 'host'), name)
            self.assertIsInstance(commodities, tuple, name)
            self.assertTrue(surfaces, name)
            if role != 'ore':
                self.assertEqual(commodities, (), '%s is %s and implies no commodity' % (name, role))

    def test_every_surface_form_reads_back_to_its_own_entry(self):
        for name, role, commodities, surfaces in composition.MINERALS:
            for s in surfaces:
                comp = composition.compose('The vein carries %s here.' % s)
                names = [m['name'] for m in comp['minerals']]
                self.assertEqual(names, [name], '%r read as %r' % (s, names))
                self.assertEqual(comp['minerals'][0]['as_written'], s)

    def test_historic_synonyms_are_one_mineral(self):
        for written in ('zinc blende', 'blende', 'black jack'):
            got = read('The ore is %s.' % written)['minerals']
            self.assertEqual([m['name'] for m in got], ['sphalerite'], written)
            self.assertEqual(got[0]['as_written'], written)
        self.assertEqual(read('Some gray copper occurs.')['minerals'][0]['name'], 'tetrahedrite')
        self.assertEqual(read('Horn silver was found.')['minerals'][0]['name'], 'cerargyrite')
        self.assertEqual(read('A little mispickel.')['minerals'][0]['name'], 'arsenopyrite')
        self.assertEqual(read('Gangue of heavy spar.')['minerals'][0]['name'], 'barite')

    def test_bare_metal_words_are_not_minerals(self):
        # "20 ounces silver" is an assay and "Gold Hill" is a name; only the
        # mineral phrases are occurrences
        comp = read('The ore ran 20 ounces silver and $8 in gold to the ton at the Gold Hill mine.')
        self.assertEqual(comp['minerals'], [])
        self.assertEqual([m['name'] for m in read('Native gold and wire silver were seen.')['minerals']],
                         ['native gold', 'native silver'])

    def test_a_rock_name_that_contains_a_mineral_name_is_the_rock(self):
        comp = read('The vein lies in quartz monzonite and quartzite near the granite contact zone.')
        self.assertEqual([h['name'] for h in comp['host_rock']],
                         ['quartz monzonite', 'quartzite', 'granite'])
        self.assertEqual(comp['minerals'], [])
        self.assertEqual([a['name'] for a in comp['alteration']], ['contact metamorphic'])

    def test_plurals_read_as_their_singular(self):
        comp = read('Ore shoots carry sulphides, garnets and stringers of pyrites in lenses.')
        self.assertEqual([m['name'] for m in comp['minerals']], ['garnet', 'pyrite'])
        self.assertEqual([t['name'] for t in comp['ore_terms']], ['ore shoot', 'stringer', 'lens'])
        self.assertEqual([a['name'] for a in comp['alteration']], ['sulphide'])


class ComposeTests(unittest.TestCase):
    def test_the_paragraph_reads_as_the_task_describes(self):
        comp = read()
        for name in ('galena', 'sphalerite', 'chalcopyrite'):
            m = by_name(comp, name)[0]
            self.assertEqual((m['role'], m['level']), ('ore', '300'), name)
        self.assertEqual({m['name']: m['role'] for m in comp['minerals'] if m['level'] == '300'
                          and m['role'] == 'gangue'}, {'quartz': 'gangue', 'calcite': 'gangue'})
        for name in ('cerussite', 'limonite'):
            m = by_name(comp, name)[0]
            self.assertEqual((m['level'], m['zone']), ('100', 'oxidized'), name)
        self.assertEqual([c['commodity'] for c in comp['commodities']], ['lead', 'zinc', 'copper'])
        self.assertNotIn('ore', [c['commodity'] for c in comp['commodities']])
        self.assertEqual(sorted(comp['by_level']), ['100', '300'])
        self.assertEqual(comp['by_level']['300'], ['galena', 'sphalerite', 'chalcopyrite', 'quartz', 'calcite'])
        self.assertEqual(comp['by_level']['100'], ['cerussite', 'limonite'])

    def test_galena_implies_lead_and_quartz_implies_nothing(self):
        comp = read()
        self.assertEqual(by_name(comp, 'galena')[0]['commodity'], 'lead')
        self.assertIsNone(by_name(comp, 'quartz')[0]['commodity'])
        lead = [c for c in comp['commodities'] if c['commodity'] == 'lead'][0]
        self.assertEqual((lead['count'], lead['minerals']), (2, ['galena', 'cerussite']))

    def test_spans_index_back_into_the_text_exactly(self):
        comp = read()
        for group in ('minerals', 'host_rock', 'alteration', 'ore_terms'):
            for rec in comp[group]:
                a, b = rec['at']
                self.assertEqual(TEXT[a:b], rec['as_written'])
                s, e = rec['span']
                self.assertEqual(' '.join(TEXT[s:e].split()), rec['quote'])
                self.assertTrue(s <= a < b <= e)
        for st in comp['statements']:
            s, e = st['span']
            self.assertEqual(' '.join(TEXT[s:e].split()), st['quote'])

    def test_a_text_with_no_minerals_is_empty_lists_and_counts(self):
        comp = composition.compose('The Main shaft was sunk 620 feet. An adit was driven N45E 900 feet.')
        for group in ('minerals', 'host_rock', 'alteration', 'ore_terms', 'statements', 'commodities'):
            self.assertEqual(comp[group], [], group)
        self.assertEqual(comp['by_level'], {})
        self.assertEqual(comp['coverage'], {'sentences': 2, 'sentences_with_minerals': 0})

    def test_empty_text(self):
        comp = composition.compose('')
        self.assertEqual(comp['minerals'], [])
        self.assertEqual(comp['coverage'], {'sentences': 0, 'sentences_with_minerals': 0})

    def test_coverage_counts_sentences(self):
        self.assertEqual(read()['coverage'], {'sentences': 4, 'sentences_with_minerals': 2})

    def test_the_zone_is_the_word_nearest_the_mineral(self):
        comp = read('The oxidized ore, with cerussite, passes at the 200 level into sulphides, '
                    'chiefly galena.')
        self.assertEqual(by_name(comp, 'cerussite')[0]['zone'], 'oxidized')
        self.assertEqual(by_name(comp, 'galena')[0]['zone'], 'sulphide')
        self.assertEqual(comp['statements'][0]['zone'], 'oxidized/sulphide')
        self.assertIsNone(read('The vein carries galena.')['minerals'][0]['zone'])

    def test_a_level_named_by_the_clause_before_ties_the_statement(self):
        comp = read('On the 400 level the vein was stoped for 200 feet; the ore is galena.')
        st = comp['statements'][0]
        self.assertEqual((st['level'], st['level_source']), ('400', 'window'))
        # but a full stop ends the window: the next sentence is on its own
        comp = read('On the 400 level the vein was stoped for 200 feet. The ore is galena.')
        self.assertIsNone(comp['statements'][0]['level'])
        self.assertEqual(comp['by_level'], {})

    def test_level_labels_follow_narrative(self):
        self.assertEqual(read('On No. 3 level the ore is galena.')['by_level'], {'No. 3': ['galena']})
        self.assertEqual(read('On the adit level the ore is galena.')['by_level'], {'adit': ['galena']})
        self.assertEqual(read('On the 1,200-foot level the ore is galena.')['by_level'], {'1200': ['galena']})

    def test_the_element_is_the_one_read_from_the_same_sentence(self):
        text = 'The Main shaft is 620 feet deep. A crosscut on the 300 level cut galena 90 feet from the shaft.'
        spec = narrative.parse(text)
        comp = composition.compose(text, spec)
        crosscut = [e for e in spec['elements'] if e['kind'] == 'crosscut'][0]
        self.assertEqual(comp['minerals'][0]['element'], crosscut['id'])
        self.assertEqual(comp['statements'][0]['element'], crosscut['id'])
        # the shaft's sentence names no mineral, and nothing is tied to it
        self.assertEqual([m['element'] for m in read()['minerals']], [None] * 7)

    def test_compose_is_deterministic(self):
        spec = narrative.parse(TEXT)
        a = json.dumps(composition.compose(TEXT, spec), sort_keys=True)
        b = json.dumps(composition.compose(TEXT, spec), sort_keys=True)
        self.assertEqual(a, b)
        self.assertEqual(json.dumps(read('galena galena')), json.dumps(read('galena galena')))


class AttachTests(unittest.TestCase):
    def test_attach_adds_the_block_and_the_canonical_list(self):
        spec = narrative.parse(TEXT)
        out = composition.attach(spec, text=TEXT)
        self.assertNotIn('composition', spec)              # a copy, like assay.attach
        self.assertEqual(out['composition']['schema'], 'nwmm-composition/1')
        self.assertEqual(out['minerals'], ['galena', 'sphalerite', 'chalcopyrite', 'quartz', 'calcite',
                                           'cerussite', 'limonite'])
        self.assertEqual(out['coverage']['minerals'], 7)
        self.assertEqual(out['coverage']['composition_statements'], 2)
        json.dumps(out)                                     # serialisable

    def test_attach_accepts_a_composition_or_the_text_positionally(self):
        spec = narrative.parse(TEXT)
        comp = composition.compose(TEXT, spec)
        self.assertEqual(composition.attach(spec, comp)['composition'], comp)
        self.assertEqual(composition.attach(spec, TEXT)['composition'], comp)

    def test_attach_survives_the_answer_loop(self):
        spec = composition.attach(assay.attach(narrative.parse(TEXT), TEXT), text=TEXT)
        pending = narrative.unresolved(spec)
        out = narrative.apply_answers(spec, [{'id': pending[0]['id'], 'value': 90.0, 'because': 'test'}])
        self.assertEqual(out['composition'], spec['composition'])
        self.assertEqual(out['minerals'], spec['minerals'])


def built_from(text, answer=90.0, site=None):
    spec = composition.attach(assay.attach(narrative.parse(text), text), text=text)
    pending = narrative.unresolved(spec)
    if pending:
        spec = narrative.apply_answers(spec, [{'id': g['id'], 'value': answer, 'because': 'test'}
                                              for g in pending])
    return spec, agentbuild.build(spec, dict(site or SITE))


def points_of(built):
    return [o for o in built['project'].objects
            if o.kind == 'points' and (o.metadata or {}).get('schema') == 'nwmm-composition/1']


class PointTests(unittest.TestCase):
    def test_the_300_level_point_sits_at_the_crosscuts_elevation(self):
        spec, built = built_from(TEXT)
        self.assertEqual(built['composition_points'], 2)
        ps = points_of(built)[0]
        self.assertEqual(ps.role, 'samples')
        crosscut = [p for p in built['placed'] if p['kind'] == 'crosscut'][0]
        i = ps.attributes['level'].index('300')
        x, y, z = ps.point(i)
        self.assertAlmostEqual(z, crosscut['start'][2], places=3)
        self.assertAlmostEqual(z, SITE['elevation_m'] - 300 * FT, places=3)
        # the sentence named no working, so the point is the level's station
        self.assertAlmostEqual(x, crosscut['start'][0], places=3)
        self.assertAlmostEqual(y, crosscut['start'][1], places=3)
        # the station's own words: the builder names the shaft by its parsed
        # name ("Main"), exactly as it does on the crosscut's placement record
        shaft_how = crosscut['placement']
        self.assertEqual(shaft_how, 'the Main at the 300 level')
        self.assertEqual(ps.attributes['placement'][i],
                         'at the 300 level: %s; the sentence names no working' % shaft_how)
        self.assertEqual(ps.attributes['minerals'][i], 'galena, sphalerite, chalcopyrite, quartz, calcite')

    def test_the_100_level_point_sits_on_the_shaft_at_100_feet(self):
        spec, built = built_from(TEXT)
        ps = points_of(built)[0]
        i = ps.attributes['level'].index('100')
        shaft = [p for p in built['placed'] if p['kind'] == 'shaft'][0]
        x, y, z = ps.point(i)
        self.assertAlmostEqual(z, SITE['elevation_m'] - 100 * FT, places=3)
        self.assertAlmostEqual(x, shaft['start'][0], places=3)
        self.assertEqual(ps.attributes['zone'][i], 'oxidized')
        self.assertEqual(ps.attributes['confidence'][i], 'described')
        self.assertTrue(ps.attributes['quote'][i].startswith('above the 100 level'))

    def test_a_statement_naming_a_working_but_no_level_sits_at_the_collar_and_says_so(self):
        spec, built = built_from(TEXT_COLLAR)
        self.assertEqual(built['composition_points'], 3)
        ps = points_of(built)[0]
        i = ps.attributes['placement'].index('at the collar: no level stated')
        x, y, z = ps.point(i)
        self.assertAlmostEqual(x, built['collar']['x'], places=3)
        self.assertAlmostEqual(y, built['collar']['y'], places=3)
        self.assertAlmostEqual(z, built['collar']['z'], places=3)
        self.assertIsNone(ps.attributes['level'][i])
        self.assertEqual(ps.attributes['minerals'][i], 'pyrite, quartz')
        self.assertEqual(ps.attributes['zone'][i], 'sulphide')
        shaft = [e for e in spec['elements'] if e['kind'] == 'shaft'][0]
        self.assertEqual(ps.attributes['element'][i], shaft['id'])

    def test_a_statement_with_no_level_and_no_working_produces_no_point(self):
        text = TEXT + ' The district is noted for its wulfenite.'
        spec, built = built_from(text)
        self.assertEqual(built['composition_points'], 2)
        ps = points_of(built)[0]
        self.assertEqual([u['statement'] for u in ps.metadata['unplaced']], ['c3'])
        self.assertIn('kept in the manifest only', ps.metadata['unplaced'][0]['reason'])
        # it is still in the spec's block, with its quote
        self.assertEqual(spec['composition']['statements'][2]['minerals'], ['wulfenite'])

    def test_a_level_the_builder_cannot_fix_is_a_reason_not_a_point(self):
        text = 'The Main shaft is 620 feet deep. On No. 3 level the ore is galena.'
        spec, built = built_from(text)
        self.assertEqual(built['composition_points'], 0)
        self.assertEqual(points_of(built), [])   # an empty set is not added

    def test_a_level_tied_statement_on_its_own_working_sits_on_that_working(self):
        text = ('The Main shaft is 620 feet deep. On the 300 level a drift was extended 450 feet '
                'N 20 W on galena and quartz.')
        spec, built = built_from(text)
        ps = points_of(built)[0]
        drift = [p for p in built['placed'] if p['kind'] == 'drift'][0]
        x, y, z = ps.point(0)
        self.assertAlmostEqual(x, (drift['start'][0] + drift['end'][0]) / 2.0, places=3)
        self.assertAlmostEqual(y, (drift['start'][1] + drift['end'][1]) / 2.0, places=3)
        self.assertAlmostEqual(z, drift['start'][2], places=3)
        self.assertEqual(ps.attributes['placement'][0], 'at the 300 level on the drift')

    def test_no_composition_on_the_spec_adds_nothing(self):
        spec = narrative.parse('The Main shaft is 620 feet deep.')
        built = agentbuild.build(spec, dict(SITE))
        self.assertEqual(built['composition_points'], 0)
        self.assertEqual(points_of(built), [])

    def test_composition_points_without_the_builder_reads_the_placed_records(self):
        # Without the builder's station function only the placed records
        # speak: the 300 level is fixed by the crosscut that starts on it and
        # the collar by the shaft, so those two points agree with the build to
        # the millimetre; the 100 level has no placed working and is a reason,
        # not a guess.
        spec, built = built_from(TEXT_COLLAR)
        ps = composition.composition_points(spec, built['placed'])
        ref = points_of(built)[0]
        self.assertEqual(ref.n, 3)
        self.assertEqual(ps.n, 2)
        for i in range(ps.n):
            j = ref.attributes['statement'].index(ps.attributes['statement'][i])
            for a, b in zip(ps.point(i), ref.point(j)):
                self.assertAlmostEqual(a, b, places=3)
            self.assertEqual(ps.attributes['minerals'][i], ref.attributes['minerals'][j])
        self.assertEqual([(u['statement'], u['level']) for u in ps.metadata['unplaced']],
                         [('c2', '100')])
        self.assertIn('not fixed by any placed working', ps.metadata['unplaced'][0]['reason'])
        self.assertEqual(ref.metadata['unplaced'], [])

    def test_the_build_is_idempotent(self):
        _, a = built_from(TEXT_COLLAR)
        _, b = built_from(TEXT_COLLAR)
        self.assertEqual(agentbuild.content_sha256(a['project']), agentbuild.content_sha256(b['project']))


class ManifestTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='composition-test-')

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_the_manifest_carries_the_block_and_the_commodities(self):
        spec, built = built_from(TEXT)
        views = render2d.render(built)
        result = publish.publish(built, spec, SITE, views=views, target=publish.LocalTarget(self.dir),
                                 base_url='https://cdn.invalid', log=lambda *a: None)
        with open(Path(self.dir) / result['key_prefix'] / 'manifest.json') as fh:
            man = json.load(fh)
        self.assertEqual(man['composition'], spec['composition'])
        self.assertEqual([c['commodity'] for c in man['commodities']], ['lead', 'zinc', 'copper'])
        self.assertEqual(man['composition_points'], 2)
        self.assertTrue(any('composition' in n for n in man['notes']))
        # the block is the audit: every mineral still has its sentence
        for m in man['composition']['minerals']:
            self.assertTrue(m['quote'])
            self.assertEqual(TEXT[m['at'][0]:m['at'][1]], m['as_written'])

    def test_a_spec_without_a_composition_publishes_none_not_an_error(self):
        spec = narrative.parse('The Main shaft is 620 feet deep.')
        built = agentbuild.build(spec, dict(SITE))
        man = publish.manifest(built, spec, SITE, [], 'x-00000000', 'deadbeef')
        self.assertIsNone(man['composition'])
        self.assertEqual(man['commodities'], [])
        self.assertEqual(man['composition_points'], 0)


class RunBuildTests(unittest.TestCase):
    """run_build (the autopopulator's and the service's path) with no bucket
    and no network: the composition rides the held spec and the counts come
    back."""

    def setUp(self):
        import jobs as minevis_jobs
        import tools as minevis_tools
        self.tools = minevis_tools
        self.state = tempfile.mkdtemp(prefix='composition-run-')
        specs = minevis_jobs.SpecStore(self.state)
        stub = types.SimpleNamespace(root=self.state)
        self.ctx = minevis_tools.Context(stub, specs, target=publish.LocalTarget(self.state),
                                         base_url='https://cdn.invalid', zoom=13, offline=True,
                                         log=lambda *a: None)

    def tearDown(self):
        shutil.rmtree(self.state, ignore_errors=True)

    def test_run_build_returns_the_counts_after_the_question_loop(self):
        args = {'text': TEXT, 'lon': SITE['lon'], 'lat': SITE['lat'], 'name': 'Silver King',
                'elevation_m': SITE['elevation_m']}
        state, result = self.tools.run_build(dict(args), self.ctx)
        self.assertEqual(state, 'questions')
        answers = [{'id': q['id'], 'value': 90.0, 'because': 'test'} for q in result['questions']]
        state, result = self.tools.run_build({'spec_id': result['spec_id'], 'answers': answers}, self.ctx)
        self.assertEqual(state, 'done', result)
        self.assertEqual(result['composition'],
                         {'minerals': 7, 'points': 2, 'commodities': ['lead', 'zinc', 'copper']})
        held = self.ctx.specs.get(result['spec_id'])['spec']
        self.assertEqual(held['minerals'][:3], ['galena', 'sphalerite', 'chalcopyrite'])

    def test_parse_mine_description_holds_the_same_composition(self):
        got = self.tools.dispatch('parse_mine_description', {'text': TEXT}, self.ctx)[1]
        self.assertEqual(got['minerals'][:2], ['galena', 'sphalerite'])
        self.assertEqual(got['composition'], composition.compose(TEXT, narrative.parse(TEXT)))
        held = self.ctx.specs.get(got['spec_id'])['spec']
        self.assertEqual(held['composition'], got['composition'])


if __name__ == '__main__':
    unittest.main()
