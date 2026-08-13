import copy
import hashlib
import json
import os
import sys
import tempfile
import unittest
from unittest import mock


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PIPELINES = os.path.join(ROOT, 'pipelines')
if PIPELINES not in sys.path:
    sys.path.insert(0, PIPELINES)

import build_national_recorder_evidence as recorder
import validate_national
from state_registry import CLAIM_STATES, load_states


def _canonical(value):
    return json.dumps(
        value, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
        allow_nan=False).encode('utf-8')


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _polygon(west, south, east, north):
    return {
        'type': 'Polygon',
        'coordinates': [[
            [west, south], [east, south], [east, north], [west, north],
            [west, south],
        ]],
    }


class RecorderFixture:
    SNAPSHOT = '2026-08-13'

    def __init__(self, root):
        self.root = root
        self.staging = os.path.join(root, 'private')
        self.publish = os.path.join(root, 'published')
        os.makedirs(self.staging)
        self.claim_codes = sorted(CLAIM_STATES)
        self.all_codes = sorted(load_states())
        self.bounds = {
            code: (-170.0 + index * 1.8, 20.0,
                   -169.0 + index * 1.8, 21.0)
            for index, code in enumerate(self.all_codes)
        }
        self.publication_documents = {}
        self.jurisdiction_documents = {}
        self.matrix_documents = {}
        self.top = {
            'schema_version': 1,
            'dataset': recorder.DATASET,
            'snapshot': self.SNAPSHOT,
            'state_clips': None,
            'publications': {},
            'jurisdictions': {},
            'portal_matrices': {},
        }
        self.inventory = os.path.join(self.staging, 'inventory.json')
        self._make_state_clips()
        self._make_jurisdictions_and_matrices()
        self._make_publications()
        self.write_top()

    @staticmethod
    def write_bytes(path, raw):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'wb') as target:
            target.write(raw)

    def write_json(self, relative, value):
        path = os.path.join(self.staging, relative)
        self.write_bytes(path, _canonical(value))
        return path

    def top_descriptor(self, relative):
        path = os.path.join(self.staging, relative)
        with open(path, 'rb') as source:
            raw = source.read()
        return {'path': relative, 'bytes': len(raw), 'sha256': _sha(raw)}

    @staticmethod
    def active_descriptor(path, relative, n):
        with open(path, 'rb') as source:
            raw = source.read()
        return {
            'file': relative,
            'format': 'geojsonseq_v1',
            'n': n,
            'bytes': len(raw),
            'sha256': _sha(raw),
            'retrieved': RecorderFixture.SNAPSHOT,
            'complete': True,
            'truncated': False,
            'total_available': n,
        }

    def write_top(self):
        self.write_bytes(self.inventory, _canonical(self.top))

    def _make_state_clips(self):
        document = {
            'schema_version': 1,
            'source': ('https://tigerweb.geo.census.gov/arcgis/rest/services/'
                       'TIGERweb/State_County/MapServer/0, January 1 2025 vintage'),
            'note': 'Synthetic unit-test geometry with authoritative schema.',
            'states': {
                code: _polygon(*self.bounds[code]) for code in self.all_codes
            },
        }
        relative = 'state-clips.json'
        self.write_json(relative, document)
        self.top['state_clips'] = self.top_descriptor(relative)

    def jurisdiction_id(self, code, suffix='001'):
        if code == 'AK':
            return ('Fairbanks Recording District' if suffix == '001' else
                    'Juneau Recording District')
        return recorder.STATE_FIPS[code] + suffix

    def _jurisdiction_feature(self, code, suffix='001', bounds=None):
        west, south, east, north = bounds or self.bounds[code]
        identifier = self.jurisdiction_id(code, suffix)
        return {
            'type': 'Feature',
            'properties': {
                'jurisdiction_id': identifier,
                'name': identifier if code == 'AK' else f'{code} Test County {suffix}',
            },
            'geometry': _polygon(west + .1, south + .1, east - .1, north - .1),
        }

    def _matrix_row(self, code, suffix='001'):
        identifier = self.jurisdiction_id(code, suffix)
        slug = identifier.lower().replace(' ', '-')
        return {
            'jurisdiction_id': identifier,
            'status': 'accepted',
            'portal_vendor': 'Reviewed Test Recorder',
            'portal_url': f'https://portal.example.test/{code.lower()}/{slug}',
            'official_url': f'https://official.example.test/{code.lower()}/{slug}',
        }

    def _make_jurisdictions_and_matrices(self):
        for code in self.claim_codes:
            kind = 'recording_district' if code == 'AK' else 'county'
            jurisdictions = {
                'schema_version': 1,
                'type': 'FeatureCollection',
                'state': code,
                'jurisdiction_type': kind,
                'authority': ('Alaska Department of Natural Resources' if code == 'AK'
                              else 'U.S. Census Bureau TIGER/Line'),
                'official_url': (
                    'https://arcgis.dnr.alaska.gov/arcgis/rest/services/OpenData/'
                    'Administrative_RecordingDistricts/MapServer/0'
                    if code == 'AK' else
                    'https://www.census.gov/geographies/mapping-files/time-series/'
                    'geo/tiger-line-file.html'),
                'retrieved': self.SNAPSHOT,
                'complete': True,
                'features': [self._jurisdiction_feature(code)],
            }
            matrix = {
                'schema_version': 1,
                'state': code,
                'jurisdiction_type': kind,
                'status': 'reviewed',
                'reviewed_on': self.SNAPSHOT,
                'reviewed_by': 'WS11 test reviewer',
                'complete': True,
                'official_directory_url': (
                    f'https://official.example.test/{code.lower()}/recorders'),
                'rows': [self._matrix_row(code)],
            }
            self.jurisdiction_documents[code] = jurisdictions
            self.matrix_documents[code] = matrix
            self.refresh_jurisdiction(code, write_top=False)
            self.refresh_matrix(code, write_top=False)

    def point_feature(self, code, system_id='federal_mlrs', feature_id=None,
                      coordinate=None):
        west, south, east, north = self.bounds[code]
        coordinate = coordinate or [(west + east) / 2, (south + north) / 2]
        feature_id = feature_id or f'{system_id}:{code}:1'
        return {
            'type': 'Feature',
            'id': feature_id,
            'properties': {
                'claim_id': f'claim-{code}-{feature_id}',
                'st': code,
                'system_id': system_id,
                'status': 'active',
            },
            'geometry': {'type': 'Point', 'coordinates': coordinate},
        }

    def _make_publications(self):
        federal_states = {}
        for code in self.claim_codes:
            relative = f'active/{code.lower()}.geojsonseq'
            path = os.path.join(self.staging, 'publications', relative)
            feature = self.point_feature(code)
            raw = _canonical(feature) + b'\n'
            self.write_bytes(path, raw)
            federal_states[code] = {
                'active': self.active_descriptor(path, relative, 1),
            }
        state_relative = 'active/ak-state.geojsonseq'
        state_path = os.path.join(self.staging, 'publications', state_relative)
        self.write_bytes(state_path, b'')
        self.publication_documents['federal_mlrs'] = {
            'schema_version': 1,
            'system_id': 'federal_mlrs',
            'source_url': recorder.SYSTEM_SOURCE_URLS['federal_mlrs'],
            'created': self.SNAPSHOT,
            'states': federal_states,
        }
        self.publication_documents['alaska_state_claims'] = {
            'schema_version': 1,
            'system_id': 'alaska_state_claims',
            'source_url': recorder.SYSTEM_SOURCE_URLS['alaska_state_claims'],
            'created': self.SNAPSHOT,
            'states': {
                'AK': {'active': self.active_descriptor(
                    state_path, state_relative, 0)},
            },
        }
        self.refresh_publication('federal_mlrs', write_top=False)
        self.refresh_publication('alaska_state_claims', write_top=False)

    def publication_relative(self, system_id):
        name = 'federal.json' if system_id == 'federal_mlrs' else 'ak-state.json'
        return f'publications/{name}'

    def refresh_publication(self, system_id, write_top=True):
        relative = self.publication_relative(system_id)
        self.write_json(relative, self.publication_documents[system_id])
        self.top['publications'][system_id] = self.top_descriptor(relative)
        if write_top:
            self.write_top()

    def set_sequence(self, system_id, code, features, **descriptor_changes):
        publication = self.publication_documents[system_id]
        relative = publication['states'][code]['active']['file']
        path = os.path.join(self.staging, 'publications', relative)
        raw = b''.join(_canonical(feature) + b'\n' for feature in features)
        self.write_bytes(path, raw)
        descriptor = self.active_descriptor(path, relative, len(features))
        descriptor.update(descriptor_changes)
        publication['states'][code]['active'] = descriptor
        self.refresh_publication(system_id)

    def refresh_jurisdiction(self, code, write_top=True):
        relative = f'jurisdictions/{code.lower()}.json'
        self.write_json(relative, self.jurisdiction_documents[code])
        self.top['jurisdictions'][code] = self.top_descriptor(relative)
        if write_top:
            self.write_top()

    def refresh_matrix(self, code, write_top=True):
        relative = f'matrices/{code.lower()}.json'
        self.write_json(relative, self.matrix_documents[code])
        self.top['portal_matrices'][code] = self.top_descriptor(relative)
        if write_top:
            self.write_top()


class RecorderEvidenceTests(unittest.TestCase):
    def fixture(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        return RecorderFixture(temporary.name)

    def test_builds_exact_content_addressed_19_state_evidence(self):
        fixture = self.fixture()
        result = recorder.build(fixture.inventory, fixture.publish)
        self.assertEqual(result['states'], 19)
        self.assertEqual(result['active_claim_features'], 19)
        checked = recorder.validate_pointer(fixture.publish)
        self.assertEqual(checked['pointer']['sha256'], result['run_sha256'])
        self.assertEqual(set(checked['run']['state_evidence']), set(CLAIM_STATES))
        ak_reference = checked['run']['state_evidence']['AK']
        self.assertEqual(
            ak_reference['bytes'],
            os.path.getsize(os.path.join(fixture.publish, ak_reference['file'])))
        with open(os.path.join(fixture.publish, ak_reference['file']),
                  encoding='utf-8') as source:
            evidence = json.load(source)
        self.assertEqual(set(evidence), {
            'schema_version', 'state', 'jurisdiction_type', 'inventory_complete',
            'active_claims',
            'live_claim_jurisdiction_ids', 'covered_jurisdiction_ids',
            'matrix_jurisdiction_ids', 'claim_systems',
        })
        self.assertEqual(evidence['jurisdiction_type'], 'recording_district')
        self.assertEqual(evidence['claim_systems'], [
            {'system_id': 'federal_mlrs',
             'active_claims': 1,
             'live_claim_jurisdiction_ids': ['Fairbanks Recording District']},
            {'system_id': 'alaska_state_claims',
             'active_claims': 0,
             'live_claim_jurisdiction_ids': []},
        ])

    def test_emitted_evidence_matches_release_validator_schema(self):
        fixture = self.fixture()
        recorder.build(fixture.inventory, fixture.publish)
        checked = recorder.validate_pointer(fixture.publish)
        for code in ('NV', 'AK'):
            reference = checked['run']['state_evidence'][code]
            jurisdiction = fixture.jurisdiction_id(code)
            systems = (['federal_mlrs', 'alaska_state_claims'] if code == 'AK'
                       else ['federal_mlrs'])
            state = {
                'regime': 'claim',
                'recorder': {
                    'jurisdiction_type': reference['jurisdiction_type'],
                    'matrix': [{
                        'jurisdiction_id': jurisdiction,
                        'status': 'accepted',
                        'portal_vendor': 'Reviewed Test Recorder',
                        'portal_url': 'https://official.example.test/portal',
                    }],
                },
                'claim_systems': [
                    {'id': system_id,
                     'source_layer_counts': {
                         'active': (0 if system_id == 'alaska_state_claims' else 1)}}
                    for system_id in systems],
            }
            acceptance = {'recorders': {
                'jurisdiction_type': reference['jurisdiction_type'],
                'inventory_complete': True,
                'active_claims': 1,
                'evidence_artifact': reference['file'],
                'evidence_sha256': reference['sha256'],
                'evidence_bytes': reference['bytes'],
                'live_claim_jurisdiction_ids': [jurisdiction],
                'covered_jurisdiction_ids': [jurisdiction],
            }}
            qa = validate_national.QA()
            with mock.patch.object(validate_national, 'SITE', fixture.publish):
                validate_national._validate_recorder_evidence(
                    qa, code, state, acceptance)
            self.assertEqual(qa.errors, [])

    def test_polygon_claim_activates_every_intersected_county(self):
        fixture = self.fixture()
        code = 'NV'
        west, south, east, north = fixture.bounds[code]
        left = fixture._jurisdiction_feature(
            code, '001', (west, south, west + .5, north))
        right = fixture._jurisdiction_feature(
            code, '003', (west + .5, south, east, north))
        left['geometry'] = _polygon(west + .1, south + .1, west + .5, north - .1)
        right['geometry'] = _polygon(west + .5, south + .1, east - .1, north - .1)
        fixture.jurisdiction_documents[code]['features'] = [left, right]
        fixture.refresh_jurisdiction(code)
        fixture.matrix_documents[code]['rows'] = [
            fixture._matrix_row(code, '001'), fixture._matrix_row(code, '003')]
        fixture.refresh_matrix(code)
        feature = fixture.point_feature(code)
        feature['geometry'] = _polygon(
            west + .44, south + .2, west + .56, north - .2)
        fixture.set_sequence('federal_mlrs', code, [feature])
        result = recorder.build(fixture.inventory, fixture.publish)
        run = recorder.validate_pointer(fixture.publish)['run']
        reference = run['state_evidence'][code]
        with open(os.path.join(fixture.publish, reference['file']),
                  encoding='utf-8') as source:
            evidence = json.load(source)
        self.assertEqual(evidence['live_claim_jurisdiction_ids'], ['32001', '32003'])
        self.assertEqual(result['live_jurisdiction_state_pairs'], 20)

    def test_authoritative_zero_active_state_emits_empty_matrix_not_fake_county(self):
        fixture = self.fixture()
        code = 'FL'
        fixture.set_sequence('federal_mlrs', code, [])
        fixture.matrix_documents[code]['rows'] = []
        fixture.refresh_matrix(code)
        result = recorder.build(fixture.inventory, fixture.publish)
        checked = recorder.validate_pointer(fixture.publish)
        reference = checked['run']['state_evidence'][code]
        self.assertEqual(reference['active_claims'], 0)
        self.assertEqual(reference['live_jurisdictions'], 0)
        with open(os.path.join(fixture.publish, reference['file']),
                  encoding='utf-8') as source:
            evidence = json.load(source)
        self.assertEqual(evidence['active_claims'], 0)
        self.assertEqual(evidence['live_claim_jurisdiction_ids'], [])
        self.assertEqual(evidence['covered_jurisdiction_ids'], [])
        self.assertEqual(evidence['matrix_jurisdiction_ids'], [])
        self.assertEqual(evidence['claim_systems'], [{
            'system_id': 'federal_mlrs', 'active_claims': 0,
            'live_claim_jurisdiction_ids': [],
        }])
        self.assertEqual(result['active_claim_features'], 18)

    def test_rejects_truncated_or_capped_active_publication(self):
        for changes in (
                {'truncated': True},
                {'total_available': 2},
                {'complete': False}):
            with self.subTest(changes=changes):
                fixture = self.fixture()
                feature = fixture.point_feature('NV')
                fixture.set_sequence('federal_mlrs', 'NV', [feature], **changes)
                with self.assertRaisesRegex(
                        recorder.PublicationError,
                        'complete, uncapped, untruncated'):
                    recorder.build(fixture.inventory, fixture.publish)

    def test_rejects_wrong_publication_state_scope(self):
        fixture = self.fixture()
        del fixture.publication_documents['federal_mlrs']['states']['WY']
        fixture.refresh_publication('federal_mlrs')
        with self.assertRaisesRegex(recorder.PublicationError,
                                   'state scope must be exactly'):
            recorder.build(fixture.inventory, fixture.publish)

    def test_rejects_non_authoritative_claim_publication_source(self):
        fixture = self.fixture()
        fixture.publication_documents['federal_mlrs']['source_url'] = (
            'https://example.test/claims')
        fixture.refresh_publication('federal_mlrs')
        with self.assertRaisesRegex(recorder.PublicationError,
                                   'not the registered authority'):
            recorder.build(fixture.inventory, fixture.publish)

    def test_rejects_off_state_claim(self):
        fixture = self.fixture()
        feature = fixture.point_feature('NV', coordinate=[0, 0])
        fixture.set_sequence('federal_mlrs', 'NV', [feature])
        with self.assertRaisesRegex(recorder.PublicationError, 'off-state'):
            recorder.build(fixture.inventory, fixture.publish)

    def test_rejects_unmapped_in_state_claim(self):
        fixture = self.fixture()
        west, south, _east, _north = fixture.bounds['NV']
        feature = fixture.point_feature('NV', coordinate=[west + .02, south + .02])
        fixture.set_sequence('federal_mlrs', 'NV', [feature])
        with self.assertRaisesRegex(recorder.PublicationError,
                                   'unmapped by authoritative'):
            recorder.build(fixture.inventory, fixture.publish)

    def test_rejects_missing_portal_for_derived_live_jurisdiction(self):
        fixture = self.fixture()
        fixture.matrix_documents['NV']['rows'] = []
        fixture.refresh_matrix('NV')
        with self.assertRaisesRegex(recorder.PublicationError,
                                   'lack accepted portal vendor'):
            recorder.build(fixture.inventory, fixture.publish)

    def test_rejects_unaccepted_or_nonofficial_portal_rows(self):
        cases = (('status', 'pending', 'status must be accepted'),
                 ('official_url', 'http://example.test', 'must be an HTTPS URL'),
                 ('portal_vendor', '', 'trimmed single-line text'))
        for field, value, message in cases:
            with self.subTest(field=field):
                fixture = self.fixture()
                fixture.matrix_documents['NV']['rows'][0][field] = value
                fixture.refresh_matrix('NV')
                with self.assertRaisesRegex(recorder.PublicationError, message):
                    recorder.build(fixture.inventory, fixture.publish)

    def test_rejects_wrong_state_county_fips(self):
        fixture = self.fixture()
        feature = fixture.jurisdiction_documents['NV']['features'][0]
        feature['properties']['jurisdiction_id'] = '06001'
        fixture.refresh_jurisdiction('NV')
        with self.assertRaisesRegex(recorder.PublicationError,
                                   'five-digit NV county FIPS'):
            recorder.build(fixture.inventory, fixture.publish)

    def test_rejects_numeric_alaska_recording_district(self):
        fixture = self.fixture()
        feature = fixture.jurisdiction_documents['AK']['features'][0]
        feature['properties']['jurisdiction_id'] = '02001'
        feature['properties']['name'] = '02001'
        fixture.refresh_jurisdiction('AK')
        with self.assertRaisesRegex(recorder.PublicationError,
                                   'Alaska DNR recording-district name'):
            recorder.build(fixture.inventory, fixture.publish)

    def test_rejects_duplicate_active_feature_identity(self):
        fixture = self.fixture()
        first = fixture.point_feature('NV', feature_id='duplicate')
        second = copy.deepcopy(first)
        second['properties']['claim_id'] = 'another-claim'
        fixture.set_sequence('federal_mlrs', 'NV', [first, second])
        with self.assertRaisesRegex(recorder.PublicationError,
                                   'duplicate feature id'):
            recorder.build(fixture.inventory, fixture.publish)

    def test_complete_zero_system_and_proven_zero_state_are_allowed(self):
        fixture = self.fixture()
        # AK state claims are a checksum-pinned, complete zero system in the
        # happy fixture; federal AK still keeps the state nonempty.
        recorder.build(fixture.inventory, fixture.publish)
        fixture = self.fixture()
        fixture.set_sequence('federal_mlrs', 'AZ', [])
        recorder.build(fixture.inventory, fixture.publish)
        run = recorder.validate_pointer(fixture.publish)['run']
        self.assertEqual(run['state_evidence']['AZ']['active_claims'], 0)
        self.assertEqual(run['state_evidence']['AZ']['live_jurisdictions'], 0)

    def test_detects_input_mutation_before_publication(self):
        fixture = self.fixture()
        artifact = fixture.publication_documents['federal_mlrs']['states']['NV'][
            'active']
        path = os.path.join(fixture.staging, 'publications', artifact['file'])

        def mutate(_context):
            with open(path, 'ab') as target:
                target.write(b'\n')

        with self.assertRaisesRegex(recorder.PublicationError,
                                   'changed during build'):
            recorder.build(fixture.inventory, fixture.publish,
                           before_commit=mutate)
        self.assertFalse(os.path.exists(os.path.join(fixture.publish, 'latest.json')))

    def test_failed_rebuild_does_not_advance_atomic_pointer(self):
        fixture = self.fixture()
        recorder.build(fixture.inventory, fixture.publish)
        pointer_path = os.path.join(fixture.publish, 'latest.json')
        with open(pointer_path, 'rb') as source:
            original = source.read()
        west, south, _east, _north = fixture.bounds['NV']
        feature = fixture.point_feature('NV', coordinate=[west + .01, south + .01])
        fixture.set_sequence('federal_mlrs', 'NV', [feature])
        with self.assertRaises(recorder.PublicationError):
            recorder.build(fixture.inventory, fixture.publish)
        with open(pointer_path, 'rb') as source:
            self.assertEqual(source.read(), original)

    def test_immutable_publication_is_idempotent_and_detects_collision(self):
        fixture = self.fixture()
        first = recorder.build(fixture.inventory, fixture.publish)
        second = recorder.build(fixture.inventory, fixture.publish)
        self.assertEqual(first, second)
        run = recorder.validate_pointer(fixture.publish)['run']
        evidence_path = os.path.join(
            fixture.publish, run['state_evidence']['NV']['file'])
        with open(evidence_path, 'wb') as target:
            target.write(b'collision')
        with self.assertRaisesRegex(recorder.PublicationError,
                                   'content-addressed path collision'):
            recorder.build(fixture.inventory, fixture.publish)

    def test_rejects_duplicate_json_keys_and_nonfinite_numbers(self):
        with self.assertRaisesRegex(recorder.PublicationError, 'duplicate JSON'):
            recorder.strict_json_bytes(b'{"state":"NV","state":"AZ"}', 'x')
        with self.assertRaisesRegex(recorder.PublicationError,
                                   'non-standard JSON number'):
            recorder.strict_json_bytes(b'{"x":NaN}', 'x')

    def test_rejects_inputs_under_public_site(self):
        fixture = self.fixture()
        with mock.patch.object(recorder, 'SITE', fixture.staging):
            with self.assertRaisesRegex(recorder.PublicationError,
                                       'outside public site'):
                recorder.build(fixture.inventory, fixture.publish)


if __name__ == '__main__':
    unittest.main()
