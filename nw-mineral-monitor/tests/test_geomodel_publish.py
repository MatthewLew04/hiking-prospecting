"""geomodel.publish — content addressing, the audit manifest, and the narrow
promise this module makes about its environment."""
import json
import shutil
import sys
import tempfile
import unittest
import urllib.parse
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

    def generate_presigned_url(self, operation, Params, ExpiresIn):   # noqa: N803
        return ('https://s3.amazonaws.com/%s/%s?X-Amz-Expires=%d&X-Amz-Signature=deadbeef'
                % (Params['Bucket'], Params['Key'], ExpiresIn))


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


class StableBytesTests(unittest.TestCase):
    """Object ids carry a counter and the clock. If the hash saw them, a
    context build's republish check could never fire."""

    def project(self):
        from geomodel.model import Project, Grid2D, StratModel, farray, utm_crs

        p = Project('t', utm_crs(11, True), origin=[0, 0, 0])
        g = Grid2D(3, 3, 0, 0, 10, 10, name='Topography', role='topography')
        g.values = farray([1.0] * 9)
        p.add(g)
        g.metadata['source'] = 'grid:' + g.id
        sm = StratModel(name='strat', topography=g.id)
        sm.units = [{'name': 'u1', 'top': g.id, 'base': None}]
        p.add(sm)
        return p

    def test_two_identical_projects_hash_the_same_despite_different_ids(self):
        a, b = self.project(), self.project()
        self.assertNotEqual(a.objects[0].id, b.objects[0].id)
        self.assertEqual(agentbuild.content_sha256(a), agentbuild.content_sha256(b))

    def test_cross_references_are_canonicalised_too(self):
        blob = agentbuild.stable_bytes(self.project())
        self.assertIn(b'"topography":"o0"', blob)
        self.assertIn(b'"grid:o0"', blob)
        self.assertNotIn(b'"created"', blob)
        self.assertNotIn(b'"modified"', blob)

    def test_a_real_difference_still_changes_the_hash(self):
        a, b = self.project(), self.project()
        b.objects[0].values[0] = 99.0
        self.assertNotEqual(agentbuild.content_sha256(a), agentbuild.content_sha256(b))


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

    def test_s3_target_refuses_to_write_outside_its_prefixes(self):
        t = publish.S3Target('a-bucket', client=FakeS3())
        for bad in ('index.html', '../secrets', 'data/coverage.json', 'privatemodels/x'):
            with self.assertRaises(publish.PublishError, msg=bad):
                t.put(bad, b'x', 'text/plain')
        t.put('models/ok/plan.svg', b'<svg/>', 'image/svg+xml')
        t.put('private/models/ok/plan.svg', b'<svg/>', 'image/svg+xml')
        self.assertEqual(t.puts, ['models/ok/plan.svg', 'private/models/ok/plan.svg'])

    def test_a_target_that_cannot_presign_returns_none(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(publish.LocalTarget(d).presign('models/x/y.json', 300))

    def test_ttl_is_clamped_to_the_document_stores_range(self):
        self.assertEqual(publish.clamp_ttl(1), publish.PRESIGN_MIN)
        self.assertEqual(publish.clamp_ttl(10 ** 9), publish.PRESIGN_MAX)
        self.assertEqual(publish.clamp_ttl(None), publish.PRESIGN_TTL)
        self.assertEqual(publish.clamp_ttl('nonsense'), publish.PRESIGN_TTL)

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
        return publish.publish(self.built, self.spec, SITE, views=kw.pop('views', self.views),
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

    def test_asking_for_a_view_the_model_lacks_republishes_it(self):
        t = publish.LocalTarget(self.dir)
        first = self.go(t, views=render2d.render(self.built, views=('plan',)))
        self.assertEqual(sorted(first['views']), ['plan'])
        second = self.go(t)
        self.assertTrue(second['republished'], 'a widened view set is not a no-op')
        self.assertEqual(sorted(second['views']), ['iso', 'plan', 'section'])
        for name in ('plan.svg', 'section.svg', 'iso.svg'):
            self.assertTrue(Path(self.dir, *second['key_prefix'].split('/'), name).exists(), name)

    def test_asking_for_fewer_views_than_are_published_writes_nothing(self):
        t = publish.LocalTarget(self.dir)
        self.go(t)
        writes = len(t.puts)
        second = self.go(t, views=render2d.render(self.built, views=('plan',)))
        self.assertFalse(second['republished'])
        self.assertEqual(len(t.puts), writes)
        self.assertEqual(sorted(second['views']), ['iso', 'plan', 'section'])

    def test_a_view_published_earlier_keeps_its_manifest_entry(self):
        t = publish.LocalTarget(self.dir)
        self.go(t, views=render2d.render(self.built, views=('plan',)))
        second = self.go(t, views=render2d.render(self.built, views=('section',)))
        self.assertTrue(second['republished'])
        self.assertEqual(sorted(second['views']), ['plan', 'section'])

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


class PrivateTests(unittest.TestCase):
    """Phase 5. `private/` is absent from the CloudFront read allowlist, so a
    model written there is unreachable through the distribution by
    construction; the only way in is a signed link."""

    def setUp(self):
        self.spec, self.built = prepared()
        self.views = render2d.render(self.built)
        self.client = FakeS3()
        self.target = publish.S3Target('nwmm-bucket', client=self.client)

    def go(self, **kw):
        kw.setdefault('private', True)
        return publish.publish(self.built, self.spec, SITE, views=self.views,
                               target=self.target, base_url='https://cdn.invalid',
                               log=lambda *a: None, **kw)

    def test_a_private_model_is_written_under_the_private_prefix(self):
        got = self.go()
        self.assertTrue(got['key_prefix'].startswith(publish.PRIVATE_PREFIX + '/'))
        for _, key in self.client.objects:
            self.assertTrue(key.startswith(publish.PRIVATE_PREFIX + '/'), key)

    def test_a_public_model_stays_out_of_the_private_prefix(self):
        got = self.go(private=False)
        self.assertTrue(got['key_prefix'].startswith(publish.PREFIX + '/'))
        self.assertNotIn('private', got['key_prefix'])
        self.assertEqual(got['access'], 'app-gate')

    def test_every_link_comes_back_signed(self):
        got = self.go()
        self.assertEqual(got['access'], 'presigned')
        for url in [got['project_url'], got['manifest_url']] + \
                list(got['views'].values()) + list(got['exports'].values()):
            self.assertIn('X-Amz-Expires', url)

    def test_the_viewer_url_carries_the_signed_project_url_encoded(self):
        got = self.go()
        self.assertTrue(got['model_url'].startswith('https://cdn.invalid/model3d.html?project='))
        param = got['model_url'].split('project=', 1)[1]
        self.assertNotIn('&', param, 'the signature query must not leak into the viewer URL')
        self.assertEqual(urllib.parse.unquote(param), got['project_url'])

    def test_the_expiry_is_reported_and_clamped(self):
        self.assertEqual(self.go(expires_in=600)['expires_in'], 600)
        self.assertEqual(self.go(expires_in=1)['expires_in'], publish.PRESIGN_MIN)
        self.assertEqual(self.go(expires_in=99999)['expires_in'], publish.PRESIGN_MAX)
        self.assertEqual(self.go()['expires_in'], publish.PRESIGN_TTL)
        self.assertRegex(self.go()['expires_utc'], r'^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$')

    def test_the_manifest_records_which_access_it_was_published_under(self):
        self.go()
        key = [k for _, k in self.client.objects if k.endswith('manifest.json')][0]
        man = json.loads(self.target.get(key))
        self.assertEqual(man['access'], 'presigned')
        self.assertTrue(any('presigned' in n for n in man['notes']))

    def test_the_stored_manifest_holds_keys_not_expiring_urls(self):
        self.go()
        key = [k for _, k in self.client.objects if k.endswith('manifest.json')][0]
        man = json.loads(self.target.get(key))
        for entry in man['files']:
            self.assertNotIn('http', entry['key'])
            self.assertTrue(entry['key'].startswith(publish.PRIVATE_PREFIX + '/'))

    def test_a_private_model_can_be_re_signed_without_rebuilding(self):
        first = self.go(expires_in=600)
        again = publish.sign(first['model_id'], target=self.target,
                             base_url='https://cdn.invalid', expires_in=60)
        self.assertEqual(again['model_id'], first['model_id'])
        self.assertEqual(again['expires_in'], 60)
        self.assertEqual(sorted(again['views']), sorted(first['views']))
        self.assertIn('X-Amz-Expires=60', again['project_url'])
        self.assertEqual(again['content_sha256'], first['content_sha256'])

    def test_signing_a_model_that_is_not_there_says_so(self):
        with self.assertRaises(publish.PublishError) as ctx:
            publish.sign('nope-00000000', target=self.target)
        self.assertIn('no model', str(ctx.exception))

    def test_a_target_that_cannot_sign_says_so_rather_than_implying_privacy(self):
        with tempfile.TemporaryDirectory() as d:
            got = publish.publish(self.built, self.spec, SITE, views=self.views,
                                  target=publish.LocalTarget(d), base_url='',
                                  private=True, log=lambda *a: None)
        self.assertEqual(got['access'], 'presigned')
        self.assertFalse(got['project_url'].startswith('http'))
        self.assertIn('cannot mint signed links', got['note'])

    def test_republishing_a_private_model_still_re_signs_the_links(self):
        first = self.go()
        second = self.go()
        self.assertFalse(second['republished'])
        self.assertIn('X-Amz-Expires', second['project_url'])
        self.assertEqual(first['model_id'], second['model_id'])


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
