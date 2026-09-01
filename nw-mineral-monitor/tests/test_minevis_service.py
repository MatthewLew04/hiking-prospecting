"""services/minevis — the HTTP surface, the job lifecycle, and the promise that
a restart resumes a build rather than losing it.

Everything here runs in-process on an ephemeral port and never touches the
network: terrain sampling is stubbed, and models are published to a temporary
directory through the same LocalTarget the service falls back to when no
bucket is configured.
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'pipelines'))
sys.path.insert(0, str(ROOT / 'services'))

from geomodel import resolve  # noqa: E402
from minevis import jobs as jobs_mod, tools as tools_mod  # noqa: E402
from minevis.server import Service, make_server  # noqa: E402

COLLAR_Z = 1900.0

PROSE = ('The Main shaft was sunk to a depth of 620 feet. '
         'An adit driven N45E for 900 feet cuts the vein. '
         'On the 300 level a drift was extended 450 feet.')


class Client(object):
    """The agent's side of the wire."""

    def __init__(self, port, token=None):
        self.port = port
        self.token = token

    def _open(self, path, data=None, token=True, method=None):
        headers = {'Content-Type': 'application/json'}
        if self.token and token:
            headers['X-MineVis-Token'] = self.token
        req = urllib.request.Request('http://127.0.0.1:%d%s' % (self.port, path),
                                     data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.status, resp.read(), dict(resp.headers)
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read(), dict(exc.headers)

    def get(self, path, token=True):
        code, body, headers = self._open(path, token=token)
        return code, _maybe_json(body), headers

    def call(self, name, arguments=None, token=True, raw=None):
        data = raw if raw is not None else json.dumps(
            {'name': name, 'arguments': arguments or {}}).encode('utf-8')
        code, body, headers = self._open('/call', data=data, token=token)
        return code, _maybe_json(body), headers

    def wait(self, job_id, timeout=60.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            code, rec, _ = self.call('get_job', {'job_id': job_id})
            if rec.get('state') in ('done', 'questions', 'error'):
                return rec
            time.sleep(0.05)
        raise AssertionError('job %s never settled (last state %r)' % (job_id, rec.get('state')))


def _maybe_json(body):
    try:
        return json.loads(body.decode('utf-8'))
    except (ValueError, UnicodeDecodeError):
        return body


class ServiceCase(unittest.TestCase):
    """One service, one ephemeral port, no network."""

    TOKEN = None

    @classmethod
    def setUpClass(cls):
        cls.state = tempfile.mkdtemp(prefix='minevis-test-')
        cls._patch = mock.patch.object(resolve, 'elevation', lambda *a, **k: COLLAR_Z)
        cls._patch.start()
        cls.service = Service(cls.state, workers=2, token=cls.TOKEN, offline=True,
                              base_url='https://example.invalid', log=lambda *a: None)
        cls.httpd = make_server(cls.service, '127.0.0.1', 0)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.client = Client(cls.port, cls.TOKEN)

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.service.close()
        cls._patch.stop()
        shutil.rmtree(cls.state, ignore_errors=True)


class ToolSchemaTests(ServiceCase):
    """GET /tools must be pasteable into an OpenAI-style `tools` parameter."""

    def test_tools_are_served_and_are_json(self):
        code, tools, headers = self.client.get('/tools')
        self.assertEqual(code, 200)
        self.assertEqual(headers['Content-Type'], 'application/json')
        self.assertIsInstance(tools, list)
        self.assertEqual(len(tools), len(tools_mod.TOOLS))

    def test_every_tool_matches_the_openai_function_shape(self):
        _, tools, _ = self.client.get('/tools')
        for tool in tools:
            self.assertEqual(sorted(tool), ['function', 'type'])
            self.assertEqual(tool['type'], 'function')
            fn = tool['function']
            self.assertEqual(sorted(fn), ['description', 'name', 'parameters'])
            self.assertRegex(fn['name'], r'^[a-z][a-z0-9_]{0,63}$')
            self.assertGreater(len(fn['description']), 40, fn['name'])
            params = fn['parameters']
            self.assertEqual(params['type'], 'object')
            self.assertIsInstance(params['properties'], dict)
            self.assertIs(params['additionalProperties'], False)
            for req in params['required']:
                self.assertIn(req, params['properties'], fn['name'])
            for prop, schema in params['properties'].items():
                self.assertIsInstance(schema, dict, '%s.%s' % (fn['name'], prop))
                self.assertTrue(schema.get('description'), '%s.%s' % (fn['name'], prop))

    def test_tool_names_are_unique_and_documented(self):
        names = [t['function']['name'] for t in tools_mod.TOOLS]
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(sorted(names), sorted(tools_mod.TOOL_NAMES))
        self.assertEqual(sorted(names),
                         sorted(list(tools_mod.SYNC) + list(tools_mod.ASYNC)))

    def test_the_array_survives_a_json_round_trip_unchanged(self):
        self.assertEqual(json.loads(json.dumps(tools_mod.TOOLS)), tools_mod.TOOLS)


class RoutingTests(ServiceCase):
    def test_healthz_reports_the_storage_it_is_actually_using(self):
        code, body, _ = self.client.get('/healthz')
        self.assertEqual(code, 200)
        self.assertTrue(body['ok'])
        self.assertEqual(body['storage'], 'local')

    def test_an_unknown_route_says_so_rather_than_hanging(self):
        code, body, _ = self.client.get('/nope')
        self.assertEqual(code, 404)
        self.assertEqual(body['error'], 'not_found')
        self.assertIn('/tools', body['detail'])

    def test_jobs_route_and_get_job_agree(self):
        code, sub, _ = self.client.call('build_mine_visual', {'text': PROSE, 'lon': -116.87, 'lat': 36.877})
        self.assertEqual(code, 202)
        job_id = sub['job_id']
        self.client.wait(job_id)
        code, viaroute, _ = self.client.get('/jobs/%s' % job_id)
        _, viatool, _ = self.client.call('get_job', {'job_id': job_id})
        self.assertEqual(code, 200)
        self.assertEqual(viaroute['state'], viatool['state'])
        self.assertEqual(viaroute['job_id'], job_id)

    def test_post_is_only_accepted_on_call(self):
        req = urllib.request.Request('http://127.0.0.1:%d/tools' % self.port, data=b'{}')
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=10)
        self.assertEqual(ctx.exception.code, 404)


class ErrorEnvelopeTests(ServiceCase):
    """Every failure is a JSON object with `error` and `detail` — never a
    traceback, and never a 200 with a broken body."""

    def test_malformed_json_is_rejected(self):
        code, body, _ = self.client.call(None, raw=b'{not json')
        self.assertEqual(code, 400)
        self.assertEqual(body['error'], 'bad_json')

    def test_a_non_object_body_is_rejected(self):
        code, body, _ = self.client.call(None, raw=b'[1, 2, 3]')
        self.assertEqual(code, 400)
        self.assertEqual(body['error'], 'bad_request')

    def test_an_unknown_tool_lists_the_real_ones(self):
        code, body, _ = self.client.call('make_me_a_sandwich', {})
        self.assertEqual(code, 400)
        self.assertEqual(body['error'], 'bad_call')
        for name in tools_mod.TOOL_NAMES:
            self.assertIn(name, body['detail'])

    def test_a_build_with_neither_text_nor_spec_id_is_refused_before_it_costs_a_job(self):
        code, body, _ = self.client.call('build_mine_visual', {'mine_id': 'grades:17'})
        self.assertEqual(code, 400)
        self.assertIn('text or spec_id', body['detail'])

    def test_text_and_spec_id_together_are_refused(self):
        code, body, _ = self.client.call('build_mine_visual',
                                         {'text': PROSE, 'spec_id': 's12345678'})
        self.assertEqual(code, 400)
        self.assertIn('not both', body['detail'])

    def test_a_lone_coordinate_is_refused(self):
        code, body, _ = self.client.call('build_mine_visual', {'text': PROSE, 'lon': -116.87})
        self.assertEqual(code, 400)
        self.assertIn('together', body['detail'])

    def test_an_unknown_view_is_named(self):
        code, body, _ = self.client.call('build_mine_visual',
                                         {'text': PROSE, 'lon': -1.0, 'lat': 1.0,
                                          'views': ['plan', 'fishtank']})
        self.assertEqual(code, 400)
        self.assertIn('fishtank', body['detail'])

    def test_a_bad_job_id_is_a_400_not_a_500(self):
        code, body, _ = self.client.call('get_job', {'job_id': '../../etc/passwd'})
        self.assertEqual(code, 400)
        self.assertEqual(body['error'], 'bad_call')

    def test_an_unknown_job_id_says_so(self):
        code, body, _ = self.client.call('get_job', {'job_id': 'j-' + '0' * 16})
        self.assertEqual(code, 400)
        self.assertIn('no such job', body['detail'])

    def test_an_oversized_body_is_refused(self):
        blob = json.dumps({'name': 'parse_mine_description',
                           'arguments': {'text': 'x' * (3 * 1024 * 1024)}}).encode('utf-8')
        code, body, _ = self.client.call(None, raw=blob)
        self.assertEqual(code, 413)
        self.assertEqual(body['error'], 'too_large')

    def test_a_mine_that_cannot_be_placed_fails_the_job_not_the_service(self):
        with mock.patch.object(resolve, 'elevation', lambda *a, **k: None):
            _, sub, _ = self.client.call('build_mine_visual',
                                         {'text': 'A shaft was sunk 300 feet.',
                                          'lon': -116.87, 'lat': 36.877})
            rec = self.client.wait(sub['job_id'])
        self.assertEqual(rec['state'], 'error')
        self.assertEqual(rec['error'], 'unplaceable')
        self.assertIn('terrain', rec['detail'])
        self.assertEqual(self.client.get('/healthz')[1]['ok'], True)

    def test_an_answer_to_a_question_that_does_not_exist_is_reported(self):
        _, sub, _ = self.client.call('build_mine_visual',
                                     {'text': PROSE, 'lon': -116.87, 'lat': 36.877})
        asked = self.client.wait(sub['job_id'])
        _, sub2, _ = self.client.call('build_mine_visual',
                                      {'spec_id': asked['spec_id'],
                                       'answers': [{'id': 'g999', 'value': 1.0}]})
        rec = self.client.wait(sub2['job_id'])
        self.assertEqual(rec['state'], 'error')
        self.assertEqual(rec['error'], 'bad_answer')


class KeepAliveTests(ServiceCase):
    """The service speaks HTTP/1.1, so an error reply that leaves the request
    body in the socket makes the *next* request on that connection fail."""

    def _conn(self):
        import http.client
        return http.client.HTTPConnection('127.0.0.1', self.port, timeout=30)

    def _post(self, conn, path, payload):
        body = json.dumps(payload).encode()
        headers = {'Content-Type': 'application/json'}
        tok = getattr(self.client, 'token', None)
        if tok:
            headers['X-MineVis-Token'] = tok
        conn.request('POST', path, body=body, headers=headers)
        resp = conn.getresponse()
        return resp.status, resp.read()

    def test_a_404_does_not_poison_the_connection(self):
        conn = self._conn()
        try:
            code, _ = self._post(conn, '/nope', {'name': 'mine_lookup', 'arguments': {'name': 'x'}})
            self.assertEqual(code, 404)
            code, body = self._post(conn, '/call', {'name': 'mine_lookup', 'arguments': {'name': 'Bluebird'}})
            self.assertEqual(code, 200, body[:200])
            self.assertIn('candidates', json.loads(body.decode()))
        finally:
            conn.close()

    def test_a_bad_content_length_does_not_poison_the_connection(self):
        import http.client
        conn = self._conn()
        try:
            conn.putrequest('POST', '/call')
            conn.putheader('Content-Type', 'application/json')
            conn.putheader('Content-Length', 'banana')
            tok = getattr(self.client, 'token', None)
            if tok:
                conn.putheader('X-MineVis-Token', tok)
            conn.endheaders()
            conn.send(b'{"name": "mine_lookup", "arguments": {"name": "x"}}')
            resp = conn.getresponse()
            self.assertEqual(resp.status, 400)
            resp.read()
        except http.client.HTTPException:
            pass
        finally:
            conn.close()
        # a fresh connection must still be served — the service is not wedged
        code, body, _ = self.client.call('mine_lookup', {'name': 'Bluebird'})
        self.assertEqual(code, 200)


#: the stope reaches the builder with neither a bearing nor a back height, so
#: it is refused twice — the two questions only the *builder* can raise.
STOPE_PROSE = ('The Main shaft was sunk to a depth of 620 feet. '
               'Stopes above the 200 level extend 350 feet along the strike.')


class PlacementQuestionTests(ServiceCase):
    """A question the builder raises has to be answerable by the agent it was
    asked of.  These questions are not in the parsed spec — they are discovered
    during placement — so answering one used to come back 'unknown gap id'."""

    def _ask(self, **args):
        _, sub, _ = self.client.call('build_mine_visual', args)
        return self.client.wait(sub['job_id'])

    def test_a_placement_question_names_the_field_it_is_missing(self):
        asked = self._ask(text=STOPE_PROSE, lon=-116.87, lat=36.877)
        self.assertEqual(asked['state'], 'questions')
        gap = asked['questions'][0]
        self.assertEqual(gap['kind'], 'placement')
        self.assertEqual(gap['field'], 'bearing_deg')
        self.assertIn(None, [o['value'] for o in gap['options']])

    def test_answering_a_placement_question_is_accepted(self):
        asked = self._ask(text=STOPE_PROSE, lon=-116.87, lat=36.877)
        gap = asked['questions'][0]
        again = self._ask(spec_id=asked['spec_id'],
                          answers=[{'id': gap['id'], 'value': 45.0, 'because': 'the vein strike'}])
        self.assertNotEqual(again.get('error'), 'bad_answer')

    def test_the_question_loop_converges_on_a_model(self):
        asked = self._ask(text=STOPE_PROSE, lon=-116.87, lat=36.877)
        spec_id, seen = asked['spec_id'], []
        for _ in range(6):
            if asked['state'] != 'questions':
                break
            gap = asked['questions'][0]
            self.assertNotIn(gap['id'], seen, 'the same question was asked twice')
            seen.append(gap['id'])
            value = 45.0 if gap['field'] == 'bearing_deg' else 30.0
            asked = self._ask(spec_id=spec_id, answers=[{'id': gap['id'], 'value': value}])
        self.assertEqual(asked['state'], 'done', asked.get('detail', ''))
        self.assertEqual(sorted(seen), ['p-e2-bearing_deg', 'p-e2-height_m'])
        self.assertEqual(asked['confidence']['assumed'], 1)
        self.assertIn('model3d.html?project=', asked['model_url'])

    def test_a_placement_answer_of_null_omits_the_element(self):
        asked = self._ask(text=STOPE_PROSE, lon=-116.87, lat=36.877)
        gap = asked['questions'][0]
        done = self._ask(spec_id=asked['spec_id'],
                         answers=[{'id': gap['id'], 'value': None, 'because': 'not stated'}])
        self.assertEqual(done['state'], 'done', done.get('detail', ''))
        self.assertEqual(done['summary']['by_type'].get('stope'), None)

    def test_resending_the_same_answer_is_not_an_error(self):
        asked = self._ask(text=STOPE_PROSE, lon=-116.87, lat=36.877)
        gap = asked['questions'][0]
        first = self._ask(spec_id=asked['spec_id'], answers=[{'id': gap['id'], 'value': 45.0}])
        again = self._ask(spec_id=asked['spec_id'], answers=[{'id': gap['id'], 'value': 45.0}])
        self.assertNotEqual(again.get('error'), 'bad_answer')
        self.assertEqual(first['state'], again['state'])


class SyncToolTests(ServiceCase):
    def test_mine_lookup_returns_candidates_and_a_question_when_ambiguous(self):
        code, body, _ = self.client.call('mine_lookup', {'name': 'Bluebird'})
        self.assertEqual(code, 200)
        self.assertGreater(len(body['candidates']), 1)
        self.assertTrue(body['ambiguous'])
        self.assertEqual(body['question']['kind'], 'which_mine')
        self.assertIn(None, [o['value'] for o in body['question']['options']])

    def test_mine_lookup_needs_a_name(self):
        code, body, _ = self.client.call('mine_lookup', {})
        self.assertEqual(code, 400)
        self.assertIn('needs a name', body['detail'])

    def test_parse_is_synchronous_and_hands_back_a_reusable_spec_id(self):
        code, body, _ = self.client.call('parse_mine_description', {'text': PROSE})
        self.assertEqual(code, 200)
        self.assertRegex(body['spec_id'], r'^s[0-9a-f]{8}$')
        self.assertEqual(len(body['elements']), 3)
        self.assertTrue(any(g['field'] == 'bearing_deg' for g in body['gaps']))
        self.assertIsNotNone(self.service.specs.get(body['spec_id']))

    def test_parse_reports_workings_that_are_named_but_not_described(self):
        code, body, _ = self.client.call('parse_mine_description', {
            'text': 'The mine is developed by two adits and a vertical shaft. '
                    'The shaft was sunk to a depth of 640 feet.'})
        self.assertEqual(code, 200)
        self.assertEqual([(m['kind'], m['count']) for m in body['mentions']],
                         [('adit', 2), ('shaft', 1)])
        self.assertEqual([e['kind'] for e in body['elements']], ['shaft'])
        self.assertEqual(body['coverage']['unresolved'], 0,
                         'a mention must not become a required question')

    def test_parse_refuses_empty_text(self):
        code, body, _ = self.client.call('parse_mine_description', {'text': '   '})
        self.assertEqual(code, 400)

    def test_list_mine_documents_reports_what_the_index_actually_holds(self):
        code, body, _ = self.client.call('list_mine_documents', {'name': 'St. Louis Mine'})
        self.assertEqual(code, 200)
        self.assertTrue(body['available'])
        self.assertIn('index_documents', body)
        self.assertTrue(all('source_url' in d for d in body['documents']))

    def test_list_mine_documents_needs_something_to_look_for(self):
        code, _, _ = self.client.call('list_mine_documents', {})
        self.assertEqual(code, 400)


PLATE = {'plate_id': 'p3', 'image': 'plate3.png', 'width': 1000, 'height': 800,
         'plane': 'plan',
         'control': [[100, 700, -116.8700, 36.8760], [900, 700, -116.8600, 36.8760],
                     [100, 100, -116.8700, 36.8820]],
         'level': '300', 'elevation_m': 1025.0,
         'source': {'doc': 'USGS Bulletin 723', 'page': '147', 'figure': 'Plate 3'},
         'traces': [{'id': 't1', 'kind': 'drift', 'name': '300 level drift',
                     'points': [[150, 650], [500, 400], [800, 300]]}]}


class MapPlateTests(ServiceCase):
    """Tracing off a georeferenced plate is the only route to `surveyed`."""

    def test_check_map_plate_reports_the_scale_and_says_it_is_usable(self):
        code, body, _ = self.client.call('check_map_plate', {'plate': PLATE})
        self.assertEqual(code, 200)
        self.assertTrue(body['usable'])
        self.assertAlmostEqual(body['scale']['m_per_px'], 1.11, delta=0.02)
        self.assertLess(body['scale']['residual_m'], 1e-6)
        self.assertIn('surveyed', body['note'])
        self.assertEqual(body['citation'], 'traced from Plate 3, USGS Bulletin 723, p. 147')

    def test_a_plate_can_be_checked_before_anything_is_traced_on_it(self):
        plate = dict(PLATE)
        plate.pop('traces')
        code, body, _ = self.client.call('check_map_plate', {'plate': plate})
        self.assertEqual(code, 200)
        self.assertTrue(body['usable'])
        self.assertEqual(body['traces'], 0)
        self.assertAlmostEqual(body['scale']['m_per_px'], 1.11, delta=0.02)

    def test_a_plate_missing_its_georeference_comes_back_as_a_question(self):
        plate = dict(PLATE)
        plate.pop('control')
        code, body, _ = self.client.call('check_map_plate', {'plate': plate})
        self.assertEqual(code, 200)
        self.assertFalse(body['usable'])
        self.assertTrue(any(q['required'] for q in body['questions']))
        self.assertNotIn('scale', body)

    def test_a_georeference_that_cannot_be_solved_is_a_400_not_a_500(self):
        collinear = dict(PLATE, control=[[100, 700, -116.8700, 36.8760],
                                         [500, 700, -116.8660, 36.8760],
                                         [900, 700, -116.8620, 36.8760]])
        code, body, _ = self.client.call('check_map_plate', {'plate': collinear})
        self.assertEqual(code, 400)
        self.assertEqual(body['error'], 'bad_call')
        self.assertIn('one line', body['detail'])

    def test_a_malformed_plate_is_a_400_with_a_reason(self):
        code, body, _ = self.client.call('check_map_plate', {'plate': dict(PLATE, width=0)})
        self.assertEqual(code, 400)
        self.assertEqual(body['error'], 'bad_call')
        self.assertIn('pixel size', body['detail'])

    def test_check_map_plate_needs_a_plate(self):
        self.assertEqual(self.client.call('check_map_plate', {})[0], 400)

    def test_a_traced_plate_builds_at_surveyed_confidence(self):
        _, sub, _ = self.client.call('build_mine_visual', {
            'text': 'The Main shaft was sunk to a depth of 620 feet. '
                    'An adit driven N45E for 900 feet cuts the vein.',
            'lon': -116.87, 'lat': 36.877, 'plates': [PLATE]})
        done = self.client.wait(sub['job_id'])
        self.assertEqual(done['state'], 'done')
        self.assertEqual(done['confidence'], {'surveyed': 1, 'described': 2, 'assumed': 0})

        code, man, _ = self.client.get(done['manifest_url'].replace('https://example.invalid', ''))
        self.assertEqual(code, 200)
        self.assertEqual(len(man['plates']), 1)
        self.assertEqual(man['plates'][0]['source']['figure'], 'Plate 3')
        self.assertEqual(man['plates'][0]['traces'][0]['points'], 3)
        traced = [e for e in man['elements'] if e['confidence'] == 'surveyed']
        self.assertEqual([e['id'] for e in traced], ['e-p3-t1'])
        self.assertIn('traced from Plate 3', traced[0]['quote'])

    def test_resending_a_plate_does_not_trace_it_twice(self):
        args = {'text': 'The Main shaft was sunk 620 feet.', 'lon': -116.87, 'lat': 36.877,
                'plates': [PLATE]}
        first = self.client.wait(self.client.call('build_mine_visual', dict(args))[1]['job_id'])
        second = self.client.wait(self.client.call('build_mine_visual', dict(args))[1]['job_id'])
        self.assertEqual(first['state'], 'done')
        self.assertEqual(second['state'], 'done')
        self.assertEqual(first['model_url'], second['model_url'])
        self.assertEqual(second['confidence']['surveyed'], 1)

    def test_a_malformed_plate_fails_the_job_with_a_typed_error(self):
        _, sub, _ = self.client.call('build_mine_visual', {
            'text': 'The Main shaft was sunk 620 feet.', 'lon': -116.87, 'lat': 36.877,
            'plates': [dict(PLATE, plane='oblique')]})
        rec = self.client.wait(sub['job_id'])
        self.assertEqual(rec['state'], 'error')
        self.assertEqual(rec['error'], 'bad_plate')

    def test_plates_must_be_a_list(self):
        code, body, _ = self.client.call('build_mine_visual', {
            'text': 'x', 'lon': -1.0, 'lat': 1.0, 'plates': PLATE})
        self.assertEqual(code, 400)
        self.assertIn('plates must be a list', body['detail'])


ASSAY_PROSE = ('The vein strikes N45E and dips 70 degrees to the northwest. '
               'The Main shaft was sunk to a depth of 620 feet. '
               'An adit driven N45E for 900 feet cuts the vein; the ore averaged '
               '0.5 ounce gold to the ton across 3 feet. '
               'Selected samples assayed 40 ounces of silver.')


class AssayTests(ServiceCase):
    """Grades quoted in the same prose, with the basis that makes them differ."""

    def test_parse_reports_the_grades_and_the_vein(self):
        code, body, _ = self.client.call('parse_mine_description', {'text': ASSAY_PROSE})
        self.assertEqual(code, 200)
        self.assertEqual([(a['commodity'], a['value'], a['basis']) for a in body['assays']],
                         [('au', 0.5, 'average'), ('ag', 40.0, 'selected')])
        self.assertAlmostEqual(body['assays'][0]['width_m'], 3 * 0.3048, places=5)
        self.assertEqual(body['vein']['strike_deg'], 45.0)
        self.assertEqual(body['vein']['dip_deg'], 70.0)
        self.assertFalse(body['vein']['dip_direction_assumed'])
        self.assertEqual(body['coverage']['assays'], 2)

    def test_an_unnamed_metal_is_an_optional_question(self):
        code, body, _ = self.client.call('parse_mine_description', {
            'text': 'An adit was driven N45E 900 feet; assays ran 30 ounces to the ton.'})
        self.assertEqual(code, 200)
        gap = [g for g in body['gaps'] if g['kind'] == 'assay'][0]
        self.assertFalse(gap['required'])
        self.assertEqual(body['coverage']['unresolved'], 0)

    def test_a_build_reports_the_grade_points_and_the_vein(self):
        _, sub, _ = self.client.call('build_mine_visual', {
            'text': ASSAY_PROSE, 'lon': -116.87, 'lat': 36.877})
        done = self.client.wait(sub['job_id'])
        self.assertEqual(done['state'], 'done')
        self.assertEqual(done['assays'], 2)
        self.assertEqual(done['vein']['strike_deg'], 45.0)
        self.assertIn('not an interpolated', done['vein']['note'])

    def test_the_manifest_keeps_each_grade_with_its_basis_and_sentence(self):
        _, sub, _ = self.client.call('build_mine_visual', {
            'text': ASSAY_PROSE, 'lon': -116.87, 'lat': 36.877})
        done = self.client.wait(sub['job_id'])
        code, man, _ = self.client.get(done['manifest_url'].replace('https://example.invalid', ''))
        self.assertEqual(code, 200)
        self.assertEqual(len(man['assays']), 2)
        picked = [a for a in man['assays'] if a['basis'] == 'selected'][0]
        self.assertEqual(picked['commodity'], 'ag')
        self.assertIn('Selected samples', picked['quote'])
        self.assertEqual(man['vein']['strike_deg'], 45.0)
        self.assertTrue(any('selected sample' in n for n in man['notes']))

    def test_a_different_grade_makes_a_different_model(self):
        base = {'lon': -116.87, 'lat': 36.877}
        a = self.client.wait(self.client.call('build_mine_visual', dict(
            base, text='An adit driven N45E for 900 feet; the ore averaged 0.5 ounce gold.'))[1]['job_id'])
        b = self.client.wait(self.client.call('build_mine_visual', dict(
            base, text='An adit driven N45E for 900 feet; the ore averaged 2.5 ounce gold.'))[1]['job_id'])
        self.assertNotEqual(a['model_url'], b['model_url'])


class JobLifecycleTests(ServiceCase):
    def test_the_question_round_trip_ends_in_a_model(self):
        code, sub, _ = self.client.call('build_mine_visual',
                                        {'text': PROSE, 'lon': -116.87, 'lat': 36.877})
        self.assertEqual(code, 202)
        asked = self.client.wait(sub['job_id'])

        self.assertEqual(asked['state'], 'questions')
        self.assertEqual(len(asked['questions']), 1)
        question = asked['questions'][0]
        self.assertEqual(question['field'], 'bearing_deg')
        self.assertTrue(question['required'])
        self.assertTrue(question['quote'])

        _, sub2, _ = self.client.call('build_mine_visual', {
            'spec_id': asked['spec_id'],
            'answers': [{'id': question['id'], 'value': 45.0,
                         'because': 'same vein as the adit'}]})
        done = self.client.wait(sub2['job_id'])

        self.assertEqual(done['state'], 'done')
        self.assertIn('model3d.html?project=', done['model_url'])
        self.assertEqual(sorted(done['views']), ['iso', 'plan', 'section'])
        self.assertIn('model.geomodel.json', done['exports'])
        self.assertEqual(done['confidence'], {'surveyed': 0, 'described': 2, 'assumed': 1})
        self.assertEqual(done['unresolved'], [])
        self.assertEqual(done['spec_id'], asked['spec_id'])

    def test_a_complete_description_builds_without_asking_anything(self):
        _, sub, _ = self.client.call('build_mine_visual', {
            'text': 'An adit driven N45E for 900 feet. The Main shaft was sunk 620 feet.',
            'lon': -116.87, 'lat': 36.877})
        done = self.client.wait(sub['job_id'])
        self.assertEqual(done['state'], 'done')
        self.assertEqual(done['confidence']['assumed'], 0)

    def test_the_answers_are_recorded_in_the_manifest_as_assumed(self):
        _, sub, _ = self.client.call('build_mine_visual',
                                     {'text': PROSE, 'lon': -116.87, 'lat': 36.877})
        asked = self.client.wait(sub['job_id'])
        _, sub2, _ = self.client.call('build_mine_visual', {
            'spec_id': asked['spec_id'],
            'answers': [{'id': asked['questions'][0]['id'], 'value': 45.0,
                         'because': 'same vein as the adit'}]})
        done = self.client.wait(sub2['job_id'])

        code, man, _ = self.client.get(done['manifest_url'].replace('https://example.invalid', ''))
        self.assertEqual(code, 200)
        self.assertEqual(len(man['answers']), 1)
        self.assertEqual(man['answers'][0]['because'], 'same vein as the adit')
        drift = [e for e in man['elements'] if e['kind'] == 'drift'][0]
        self.assertEqual(drift['fields']['bearing_deg'], 'assumed')
        self.assertEqual(drift['confidence'], 'assumed')
        self.assertTrue(drift['quote'])
        self.assertEqual(len(drift['span']), 2)
        # and the elements that were not answered are still marked described
        adit = [e for e in man['elements'] if e['kind'] == 'adit'][0]
        self.assertEqual(adit['confidence'], 'described')

    def test_building_the_same_description_twice_returns_the_same_url(self):
        args = {'text': 'A tunnel was driven N 70 W for 640 feet.',
                'lon': -116.87, 'lat': 36.877}
        _, a, _ = self.client.call('build_mine_visual', dict(args))
        first = self.client.wait(a['job_id'])
        _, b, _ = self.client.call('build_mine_visual', dict(args))
        second = self.client.wait(b['job_id'])
        self.assertEqual(first['state'], 'done')
        self.assertEqual(second['state'], 'done')
        self.assertEqual(first['model_url'], second['model_url'])
        self.assertTrue(first['republished'])
        self.assertFalse(second['republished'], 'an unchanged rebuild must be a no-op')
        # a republished result must describe exactly the same set of files as
        # the first publish, or the agent sees the answer change under it
        self.assertEqual(first['exports'], second['exports'])
        self.assertEqual(first['views'], second['views'])
        self.assertEqual(first['content_sha256'], second['content_sha256'])
        self.assertEqual(first['manifest_url'], second['manifest_url'])

    def test_a_different_description_gets_a_different_model(self):
        _, a, _ = self.client.call('build_mine_visual',
                                   {'text': 'An adit driven N45E for 900 feet.',
                                    'lon': -116.87, 'lat': 36.877})
        _, b, _ = self.client.call('build_mine_visual',
                                   {'text': 'An adit driven N45E for 1200 feet.',
                                    'lon': -116.87, 'lat': 36.877})
        self.assertNotEqual(self.client.wait(a['job_id'])['model_url'],
                            self.client.wait(b['job_id'])['model_url'])

    def test_a_build_with_no_mine_asks_which_mine_rather_than_guessing(self):
        _, sub, _ = self.client.call('build_mine_visual', {'text': PROSE})
        rec = self.client.wait(sub['job_id'])
        self.assertEqual(rec['state'], 'questions')
        self.assertEqual(rec['questions'][0]['kind'], 'which_mine')

    def test_the_published_files_are_actually_fetchable(self):
        _, sub, _ = self.client.call('build_mine_visual',
                                     {'text': 'An adit driven due east for 400 feet.',
                                      'lon': -116.87, 'lat': 36.877})
        done = self.client.wait(sub['job_id'])
        for url in list(done['views'].values()) + list(done['exports'].values()):
            path = url.replace('https://example.invalid', '')
            code, body, headers = self.client.get(path)
            self.assertEqual(code, 200, path)
            self.assertGreater(len(body if isinstance(body, bytes) else json.dumps(body)), 200, path)
        self.assertEqual(self.client.get(
            done['views']['plan'].replace('https://example.invalid', ''))[2]['Content-Type'],
            'image/svg+xml')

    def test_asking_a_built_model_for_more_views_renders_them(self):
        args = {'text': 'An adit driven due east for 480 feet.',
                'lon': -116.87, 'lat': 36.877, 'views': ['plan']}
        first = self.client.wait(self.client.call('build_mine_visual', dict(args))[1]['job_id'])
        self.assertEqual(first['state'], 'done')
        self.assertEqual(sorted(first['views']), ['plan'])

        args['views'] = ['plan', 'section', 'iso']
        second = self.client.wait(self.client.call('build_mine_visual', dict(args))[1]['job_id'])
        self.assertEqual(second['state'], 'done')
        self.assertEqual(second['model_url'], first['model_url'])
        self.assertEqual(sorted(second['views']), ['iso', 'plan', 'section'])
        for url in second['views'].values():
            path = url.replace('https://example.invalid', '')
            self.assertEqual(self.client.get(path)[0], 200, path)

    def test_a_private_model_on_a_bucketless_box_is_reachable_through_it(self):
        _, sub, _ = self.client.call('build_mine_visual', {
            'text': 'An adit driven due west for 360 feet.', 'lon': -116.87, 'lat': 36.877,
            'private': True, 'expires_in': 600})
        done = self.client.wait(sub['job_id'])
        self.assertEqual(done['state'], 'done')
        self.assertTrue(done['key_prefix'].startswith('private/models/'))
        self.assertIn('only reachable through this service', done['note'])
        for url in ([done['project_url'], done['manifest_url']]
                    + list(done['views'].values()) + list(done['exports'].values())):
            self.assertTrue(url.startswith('/private/models/'), url)
            self.assertEqual(self.client.get(url)[0], 200, url)

    def test_a_path_outside_the_model_store_is_refused(self):
        for path in ('/models/../../etc/passwd', '/models/..%2f..%2fetc/passwd',
                     '/private/models/../../../etc/passwd', '/private/models/../jobs'):
            code, _, _ = self.client.get(path)
            self.assertIn(code, (400, 404), path)


class FakeS3(object):
    """Enough of the boto3 client to exercise the private path in-process."""

    def __init__(self):
        self.objects = {}

    def put_object(self, Bucket, Key, Body, ContentType):    # noqa: N803 - boto3 API
        self.objects[(Bucket, Key)] = Body

    def get_object(self, Bucket, Key):                       # noqa: N803 - boto3 API
        class _R(object):
            def __init__(self, d):
                self.d = d

            def read(self):
                return self.d
        return {'Body': _R(self.objects[(Bucket, Key)])}

    def generate_presigned_url(self, operation, Params, ExpiresIn):   # noqa: N803
        return ('https://s3.amazonaws.com/%s/%s?X-Amz-Expires=%d&X-Amz-Signature=deadbeef'
                % (Params['Bucket'], Params['Key'], ExpiresIn))


class PrivateModelTests(unittest.TestCase):
    """Phase 5: a model that must not be world-readable."""

    @classmethod
    def setUpClass(cls):
        from geomodel import publish

        cls.state = tempfile.mkdtemp(prefix='minevis-private-')
        cls._patch = mock.patch.object(resolve, 'elevation', lambda *a, **k: COLLAR_Z)
        cls._patch.start()
        cls.s3 = FakeS3()
        cls.service = Service(cls.state, workers=2, offline=True,
                              base_url='https://cdn.invalid', log=lambda *a: None,
                              target=publish.S3Target('nwmm-bucket', client=cls.s3))
        cls.httpd = make_server(cls.service, '127.0.0.1', 0)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.client = Client(cls.port)

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.service.close()
        cls._patch.stop()
        shutil.rmtree(cls.state, ignore_errors=True)

    def build(self, **extra):
        args = {'text': PROSE.replace('450 feet', '450 feet N45E'),
                'lon': -116.87, 'lat': 36.877}
        args.update(extra)
        code, sub, _ = self.client.call('build_mine_visual', args)
        self.assertEqual(code, 202)
        return self.client.wait(sub['job_id'])

    def test_a_private_build_returns_signed_links_and_an_expiry(self):
        done = self.build(private=True, expires_in=600)
        self.assertEqual(done['state'], 'done')
        self.assertEqual(done['access'], 'presigned')
        self.assertEqual(done['expires_in'], 600)
        self.assertTrue(done['key_prefix'].startswith('private/models/'))
        self.assertIn('X-Amz-Expires=600', done['project_url'])
        self.assertIn('stop working', done['note'])

    def test_a_public_build_is_still_the_default(self):
        done = self.build()
        self.assertEqual(done['access'], 'app-gate')
        self.assertTrue(done['key_prefix'].startswith('models/'))
        self.assertNotIn('expires_in', done)

    def test_nothing_private_is_written_under_the_public_prefix(self):
        self.build(private=True)
        private = [k for _, k in self.s3.objects if k.startswith('private/models/')]
        self.assertTrue(private)
        for _, key in self.s3.objects:
            self.assertTrue(key.startswith('models/') or key.startswith('private/models/'), key)

    def test_expired_links_can_be_re_signed_without_rebuilding(self):
        done = self.build(private=True, expires_in=600)
        code, again, _ = self.client.call('sign_model_url',
                                          {'model_id': done['model_id'], 'expires_in': 60})
        self.assertEqual(code, 200)
        self.assertEqual(again['model_id'], done['model_id'])
        self.assertEqual(again['expires_in'], 60)
        self.assertIn('X-Amz-Expires=60', again['project_url'])
        self.assertEqual(sorted(again['views']), ['iso', 'plan', 'section'])

    def test_signing_an_unknown_model_is_a_400_that_names_the_prefix(self):
        code, body, _ = self.client.call('sign_model_url', {'model_id': 'nope-00000000'})
        self.assertEqual(code, 400)
        self.assertIn('private/models', body['detail'])

    def test_sign_model_url_needs_a_model_id(self):
        self.assertEqual(self.client.call('sign_model_url', {})[0], 400)

    def test_expires_in_without_private_is_refused_rather_than_ignored(self):
        code, body, _ = self.client.call('build_mine_visual', {
            'text': 'An adit was driven N45E 900 feet.', 'lon': -116.87, 'lat': 36.877,
            'expires_in': 600})
        self.assertEqual(code, 400)
        self.assertIn('private: true', body['detail'])


class AuthTests(ServiceCase):
    TOKEN = 'shared-secret-for-the-sidecar'

    def test_a_call_without_the_token_is_rejected(self):
        code, body, _ = self.client.get('/tools', token=False)
        self.assertEqual(code, 401)
        self.assertEqual(body['error'], 'unauthorised')
        code, body, _ = self.client.call('mine_lookup', {'name': 'Bluebird'}, token=False)
        self.assertEqual(code, 401)

    def test_a_call_with_the_token_is_accepted(self):
        self.assertEqual(self.client.get('/tools')[0], 200)

    def test_a_wrong_token_is_rejected(self):
        wrong = Client(self.port, 'not-the-secret')
        self.assertEqual(wrong.get('/tools')[0], 401)

    def test_healthz_stays_open_so_systemd_can_still_see_the_process(self):
        self.assertEqual(self.client.get('/healthz', token=False)[0], 200)


class RestartTests(unittest.TestCase):
    """A job that was mid-flight when the process died is picked back up,
    because its arguments are on disk."""

    def setUp(self):
        self.state = tempfile.mkdtemp(prefix='minevis-restart-')

    def tearDown(self):
        shutil.rmtree(self.state, ignore_errors=True)

    def test_a_running_job_is_requeued_and_finished_after_a_restart(self):
        started = threading.Event()
        hang = threading.Event()

        def blocking(name, args):
            started.set()
            hang.wait(10)
            return 'done', {'ok': True}

        first = jobs_mod.JobStore(self.state, runner=blocking, log=lambda *a: None)
        job_id = first.submit('build_mine_visual', {'text': 'x'})
        self.assertTrue(started.wait(5))
        self.assertEqual(first.read(job_id)['state'], 'running')
        hang.set()
        first.close()

        # simulate the crash: the record is left mid-flight on disk
        first.update(job_id, state='running')

        ran = threading.Event()

        def quick(name, args):
            ran.set()
            return 'done', {'ok': True, 'args': args}

        second = jobs_mod.JobStore(self.state, runner=quick, log=lambda *a: None)
        self.assertEqual(second.resume(), [job_id])
        self.assertTrue(ran.wait(5))
        second.close()

        rec = second.read(job_id)
        self.assertEqual(rec['state'], 'done')
        self.assertEqual(rec['result']['args'], {'text': 'x'})
        self.assertGreaterEqual(rec['attempts'], 2)

    def test_a_job_that_keeps_dying_is_abandoned_rather_than_looping_forever(self):
        # Close the first store before submitting, so the record lands on disk
        # without a worker ever touching it; otherwise this test races the pool
        # for the job's final state.
        first = jobs_mod.JobStore(self.state, runner=lambda n, a: ('done', {}),
                                  log=lambda *a: None)
        first.close()
        job_id = first.submit('build_mine_visual', {'text': 'x'})
        first.update(job_id, state='running', attempts=3)
        self.assertEqual(first.read(job_id)['state'], 'running')

        second = jobs_mod.JobStore(self.state, runner=lambda n, a: ('done', {}),
                                   log=lambda *a: None)
        self.assertEqual(second.resume(), [], 'a job that has already failed three '
                                              'restarts must not be picked up again')
        second.close()
        rec = second.read(job_id)
        self.assertEqual(rec['state'], 'error')
        self.assertEqual(rec['result']['error'], 'abandoned')
        self.assertIn('3 times', rec['result']['detail'])

    def test_a_finished_job_is_not_run_again(self):
        calls = []
        store = jobs_mod.JobStore(self.state, runner=lambda n, a: (calls.append(n), ('done', {}))[1],
                                  log=lambda *a: None)
        job_id = store.submit('build_mine_visual', {'text': 'x'})
        for _ in range(100):
            if store.read(job_id)['state'] == 'done':
                break
            time.sleep(0.02)
        self.assertEqual(store.resume(), [])
        store.close()
        self.assertEqual(len(calls), 1)

    def test_job_records_are_written_atomically(self):
        store = jobs_mod.JobStore(self.state, runner=lambda n, a: ('done', {}), log=lambda *a: None)
        store.submit('build_mine_visual', {'text': 'x'})
        store.close()
        leftovers = [n for n in os.listdir(os.path.join(self.state, 'jobs')) if n.startswith('.tmp-')]
        self.assertEqual(leftovers, [])

    def test_a_crash_inside_a_job_never_kills_the_pool(self):
        def boom(name, args):
            raise RuntimeError('the tile server ate it')

        store = jobs_mod.JobStore(self.state, runner=boom, log=lambda *a: None)
        job_id = store.submit('build_mine_visual', {'text': 'x'})
        for _ in range(100):
            if store.read(job_id)['state'] == 'error':
                break
            time.sleep(0.02)
        store.close()
        rec = store.read(job_id)
        self.assertEqual(rec['result']['error'], 'RuntimeError')
        self.assertIn('tile server', rec['result']['detail'])


class SpecStoreTests(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.mkdtemp(prefix='minevis-specs-')

    def tearDown(self):
        shutil.rmtree(self.state, ignore_errors=True)

    def test_specs_round_trip_with_the_mine_they_were_resolved_against(self):
        store = jobs_mod.SpecStore(self.state)
        spec = {'spec_id': 's1234abcd', 'elements': [], 'gaps': []}
        store.put(spec, {'mine_id': 'grades:17'})
        held = store.get('s1234abcd')
        self.assertEqual(held['spec'], spec)
        self.assertEqual(held['site']['mine_id'], 'grades:17')

    def test_a_bogus_spec_id_cannot_reach_outside_the_store(self):
        store = jobs_mod.SpecStore(self.state)
        for bad in ('../../etc/passwd', 'sZZZZZZZZ', '', None):
            with self.assertRaises(KeyError):
                store.get(bad)

    def test_an_absent_spec_is_none_not_an_error(self):
        self.assertIsNone(jobs_mod.SpecStore(self.state).get('s00000000'))


if __name__ == '__main__':
    unittest.main()
