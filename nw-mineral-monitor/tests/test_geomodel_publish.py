"""geomodel.publish — content addressing, the audit manifest, and the narrow
promise this module makes about its environment."""
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'pipelines'))

from geomodel import agentbuild, narrative, publish, render2d  # noqa: E402

SITE = {'name': 'Silver King mine', 'mine_id': 'grades:17', 'lon': -116.87, 'lat': 36.877,
        'elevation_m': 1900.0, 'state': 'NV', 'district': 'Manhattan',
        'source': 'USGS Bulletin 723', 'source_url': 'https://example.invalid/b723',
        'quote': 'The average gross value was $19.14 a ton.'}

PROSE = ('The Main shaft was sunk to a depth of 620 feet. '
         'An adit driven N45E for 900 feet cuts the vein. '
         'On the 300 level a drift was extended 450 feet.')


def prepared(prose=PROSE, site=None, answer=45.0):
    spec = narrative.parse(prose, mine_id=(site or SITE).get('mine_id'))
    pending = narrative.unresolved(spec)
    if pending and answer is not None:
        spec = narrative.apply_answers(spec, [{'id': pending[0]['id'], 'value': answer,
                                               'because': 'same vein as the adit'}])
    built = agentbuild.build(spec, dict(site or SITE))
    return spec, built


class FakeS3(object):
    """Just enough of the boto3 client surface to see what would be sent."""

    def __init__(self):
        self.objects = {}

    def put_object(self, Bucket, Key, Body, ContentType):   # noqa: N803 - boto3 API
        self.objects[(Bucket, Key)] = (Body, ContentType)

    def get_object(self, Bucket, Key):                      # noqa: N803 - boto3 API
        body, _ = self.objects[(Bucket, Key)]
        return {'Body': _Reader(body)}


class _Reader(object):
    def __init__(self, data):
        self.data = data

    def read(self):
        return self.data


class ModelIdTests(unittest.TestCase):
    def test_the_id_is_a_slug_and_a_content_hash(self):
        spec, _ = prepared()
        mid = publish.model_id(spec, SITE)
        self.assertRegex(mid, r'^silver-king-mine-[0-9a-f]{8}$')

    def test_the_same_description_of_the_same_mine_is_the_same_id(self):
        a, _ = prepared()
        b, _ = prepared()
        self.assertEqual(publish.model_id(a, SITE), publish.model_id(b, SITE))

    def test_a_different_description_is_a_different_id(self):
        a, _ = prepared()
        b, _ = prepared(PROSE + ' A winze was sunk 120 ft below the 400 level.')
        self.assertNotEqual(publish.model_id(a, SITE), publish.model_id(b, SITE))

    def test_a_different_answer_is_a_different_id(self):
        a, _ = prepared(answer=45.0)
        b, _ = prepared(answer=90.0)
        self.assertNotEqual(publish.model_id(a, SITE), publish.model_id(b, SITE),
                            'an assumed value changes the model, so it must change the id')

    def test_a_different_mine_or_coordinate_is_a_different_id(self):
        spec, _ = prepared()
        base = publish.model_id(spec, SITE)
        self.assertNotEqual(base, publish.model_id(spec, dict(SITE, mine_id='grades:18')))
        self.assertNotEqual(base, publish.model_id(spec, dict(SITE, lon=-116.88)))
        self.assertNotEqual(base, publish.model_id(spec, dict(SITE, elevation_m=1901.0)))

    def test_a_new_builder_version_is_a_different_id(self):
        spec, _ = prepared()
        self.assertNotEqual(publish.model_id(spec, SITE),
                            publish.model_id(spec, SITE, builder_version='nwmm-agentbuild/2'))

    def test_slugify_is_safe_for_a_key(self):
        for raw, want in (('Bell & Co. No. 2', 'bell-co-no-2'),
                          ('  ...  ', 'mine'),
                          ('../../etc/passwd', 'etc-passwd'),
                          ('', 'mine')):
            self.assertEqual(publish.slugify(raw), want)


class TargetTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='publish-test-')

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_local_target_round_trips_and_records_what_it_wrote(self):
        t = publish.LocalTarget(self.dir)
        t.put('models/x/model.geomodel.json', b'{}', 'application/json')
        self.assertEqual(t.get('models/x/model.geomodel.json'), b'{}')
        self.assertEqual(t.puts, ['models/x/model.geomodel.json'])
        self.assertIsNone(t.get('models/x/missing.json'))

    def test_s3_target_refuses_to_write_outside_the_models_prefix(self):
        t = publish.S3Target('a-bucket', client=FakeS3())
        with self.assertRaises(publish.PublishError):
            t.put('index.html', b'x', 'text/html')
        with self.assertRaises(publish.PublishError):
            t.put('../secrets', b'x', 'text/plain')
        t.put('models/ok/plan.svg', b'<svg/>', 'image/svg+xml')
        self.assertEqual(t.puts, ['models/ok/plan.svg'])

    def test_s3_target_needs_a_bucket_and_says_where_to_put_one(self):
        with self.assertRaises(publish.PublishError) as ctx:
            publish.S3Target('')
        self.assertIn('NWMM_MODELS_BUCKET', str(ctx.exception))

    def test_a_missing_object_is_none_not_an_exception(self):
        self.assertIsNone(publish.S3Target('b', client=FakeS3()).get('models/nope/x.json'))


class PublishTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='publish-test-')
        self.spec, self.built = prepared()
        self.views = render2d.render(self.built)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def go(self, target=None, **kw):
        return publish.publish(self.built, self.spec, SITE, views=self.views,
                               target=target or publish.LocalTarget(self.dir),
                               base_url='https://cdn.invalid', log=lambda *a: None, **kw)

    def test_the_expected_files_are_written_in_a_stable_order(self):
        t = publish.LocalTarget(self.dir)
        result = self.go(t)
        self.assertEqual([k.rsplit('/', 1)[-1] for k in t.puts], list(publish.FILE_ORDER))
        self.assertTrue(result['republished'])

    def test_the_returned_url_is_the_one_the_viewer_understands(self):
        result = self.go()
        self.assertEqual(result['model_url'],
                         'https://cdn.invalid/model3d.html?project=/models/%s/model.geomodel.json'
                         % result['model_id'])
        self.assertEqual(result['project_url'],
                         'https://cdn.invalid/models/%s/model.geomodel.json' % result['model_id'])

    def test_without_a_base_url_the_paths_are_still_usable(self):
        result = publish.publish(self.built, self.spec, SITE, views=self.views,
                                 target=publish.LocalTarget(self.dir), base_url='',
                                 log=lambda *a: None)
        self.assertTrue(result['model_url'].startswith('model3d.html?project=/models/'))
        self.assertTrue(result['views']['plan'].startswith('/models/'))

    def test_content_types_are_set_per_extension(self):
        t = publish.S3Target('a-bucket', client=FakeS3())
        self.go(t)
        got = dict((k.rsplit('/', 1)[-1], v[1]) for (b, k), v in t._client.objects.items())
        self.assertEqual(got['model.geomodel.json'], 'application/json')
        self.assertEqual(got['workings.geojson'], 'application/geo+json')
        self.assertEqual(got['plan.svg'], 'image/svg+xml')
        self.assertEqual(got['model.omf'], 'application/octet-stream')
        self.assertEqual(got['workings.dxf'], 'application/dxf')

    def test_republishing_the_same_model_writes_nothing(self):
        t = publish.LocalTarget(self.dir)
        first = self.go(t)
        writes = len(t.puts)
        second = self.go(t)
        self.assertEqual(len(t.puts), writes, 'an unchanged republish must not re-upload')
        self.assertFalse(second['republished'])
        self.assertEqual(first['model_url'], second['model_url'])
        self.assertEqual(first['exports'], second['exports'])
        self.assertEqual(first['views'], second['views'])

    def test_force_republishes_even_when_unchanged(self):
        t = publish.LocalTarget(self.dir)
        self.go(t)
        writes = len(t.puts)
        self.assertTrue(self.go(t, force=True)['republished'])
        self.assertEqual(len(t.puts), writes + len(publish.FILE_ORDER))

    def test_a_corrupt_prior_manifest_is_republished_over(self):
        t = publish.LocalTarget(self.dir)
        result = self.go(t)
        t.put('%s/manifest.json' % result['key_prefix'], b'not json', 'application/json')
        self.assertTrue(self.go(t)['republished'])


class ManifestTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='publish-test-')
        self.spec, self.built = prepared()
        self.target = publish.LocalTarget(self.dir)
        self.result = publish.publish(self.built, self.spec, SITE,
                                      views=render2d.render(self.built),
                                      target=self.target, base_url='https://cdn.invalid',
                                      log=lambda *a: None)
        self.man = json.loads(self.target.get('%s/manifest.json' % self.result['key_prefix']))

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_the_manifest_pins_the_input_and_every_version(self):
        self.assertEqual(self.man['input']['text_sha256'], self.spec['text_sha256'])
        self.assertEqual(self.man['input']['spec_id'], self.spec['spec_id'])
        self.assertEqual(sorted(self.man['versions']),
                         ['builder', 'parser', 'publisher', 'renderer'])
        self.assertEqual(self.man['versions']['parser'], narrative.PARSER_VERSION)

    def test_every_element_is_traceable_to_its_sentence(self):
        self.assertEqual(len(self.man['elements']), len(self.spec['elements']))
        for el in self.man['elements']:
            self.assertTrue(el['quote'])
            self.assertEqual(len(el['span']), 2)
            self.assertIn(el['confidence'], ('surveyed', 'described', 'assumed'))
            self.assertTrue(el['fields'])
            if el['built']:
                self.assertTrue(el['placement'])

    def test_the_answers_are_listed_apart_from_the_elements(self):
        self.assertEqual(len(self.man['answers']), 1)
        answer = self.man['answers'][0]
        self.assertEqual(answer['field'], 'bearing_deg')
        self.assertEqual(answer['value'], 45.0)
        self.assertEqual(answer['because'], 'same vein as the adit')
        drift = [e for e in self.man['elements'] if e['kind'] == 'drift'][0]
        self.assertEqual(drift['fields']['bearing_deg'], 'assumed')

    def test_definitional_defaults_are_named(self):
        shaft = [e for e in self.man['elements'] if e['kind'] == 'shaft'][0]
        self.assertEqual(shaft['definitional_defaults'], ['dip_deg'])

    def test_the_mine_citation_travels_with_the_model(self):
        self.assertEqual(self.man['mine']['source_url'], SITE['source_url'])
        self.assertEqual(self.man['mine']['mine_id'], 'grades:17')
        self.assertEqual(self.man['mine']['elevation_m'], 1900.0)

    def test_the_file_checksums_match_what_was_written(self):
        import hashlib

        self.assertTrue(self.man['files'])
        for entry in self.man['files']:
            data = self.target.get(entry['key'])
            self.assertIsNotNone(data, entry['key'])
            self.assertEqual(len(data), entry['bytes'])
            self.assertEqual(hashlib.sha256(data).hexdigest(), entry['sha256'])

    def test_the_manifest_does_not_claim_to_checksum_itself(self):
        self.assertNotIn('manifest.json', [f['name'] for f in self.man['files']])

    def test_the_notes_say_plainly_that_this_is_not_a_survey(self):
        joined = ' '.join(self.man['notes'])
        self.assertIn('not from a survey', joined)
        self.assertIn('assumed', joined)

    def test_nothing_is_left_unresolved_in_a_completed_model(self):
        self.assertEqual(self.man['unresolved'], [])
        self.assertEqual(self.man['confidence'], {'surveyed': 0, 'described': 2, 'assumed': 1})


if __name__ == '__main__':
    unittest.main()
