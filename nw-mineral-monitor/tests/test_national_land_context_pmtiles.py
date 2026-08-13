import copy
import hashlib
import importlib
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock


ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), '..'))
PIPELINES = os.path.join(ROOT, 'pipelines')
import sys
sys.path.insert(0, PIPELINES)

landpub = importlib.import_module('build_national_land_context_pmtiles')


MI_POINT = [-85.5, 44.0]


def _square(x, y, half):
    return {
        'type': 'Polygon',
        'coordinates': [[
            [x - half, y - half], [x + half, y - half],
            [x + half, y + half], [x - half, y + half],
            [x - half, y - half],
        ]],
    }


def _feature(identity, properties, geometry):
    return {
        'type': 'Feature',
        'id': identity,
        'properties': properties,
        'geometry': geometry,
    }


def _pagination(n):
    return {
        'method': 'single_file',
        'expected_count': n,
        'fetched_count': n,
        'page_size': n,
        'page_offsets': [0],
        'page_row_counts': [n],
        'pagination_exhausted': True,
        'source_snapshot_id': 'fixture-etag-complete-v1',
    }


def _surface_snapshot(code='MI'):
    features = [_feature('SURF-1', {
        'record_id': 'SURF-1',
        'surface_class': 'state',
        'surface_manager': 'Michigan DNR',
        'source_id': 'blm_sma',
        'source_scale': '1:100,000',
        'source_ref': 'fixture surface-management polygon',
        'as_of': '2026-08-13',
        'mineral_interest_id': 'MIN-1',
    }, _square(MI_POINT[0], MI_POINT[1], 0.25))]
    return {
        'schema_version': 1,
        'state': code,
        'kind': 'surface_ownership',
        'source_ids': ['blm_sma'],
        'official_source_urls': [
            landpub.national_sources()['blm_sma']['url']],
        'retrieved': '2026-08-13',
        'complete': True,
        'truncated': False,
        'pagination': _pagination(len(features)),
        'type': 'FeatureCollection',
        'features': features,
        'evidence_domain': 'surface_management',
        'coverage_status': 'statewide_complete',
    }


def _mineral_snapshot(code='MI'):
    features = [_feature('MIN-1', {
        'record_id': 'MIN-1',
        'mineral_class': 'state',
        'confidence': 'verified',
        'evidence_basis': 'state_mineral_inventory',
        'source_id': 'mi_mineral_title_fixture',
        'source_ref': 'fixture mineral-interest record',
        'note': ('Independent mineral-interest evidence identifies this '
                 'fixture polygon as state-owned minerals.'),
    }, _square(MI_POINT[0], MI_POINT[1], 0.35))]
    return {
        'schema_version': 1,
        'state': code,
        'kind': 'mineral_interests',
        'source_ids': ['mi_mineral_title_fixture'],
        'official_source_urls': ['https://example.gov/minerals'],
        'retrieved': '2026-08-13',
        'complete': True,
        'truncated': False,
        'pagination': _pagination(len(features)),
        'type': 'FeatureCollection',
        'features': features,
        'evidence_domain': 'mineral_interest',
        'coverage_status': 'statewide_complete',
    }


def _ranked_snapshot(context_sha, code='MI'):
    points = [
        [-85.50, 44.00], [-85.45, 44.02], [-85.55, 43.98],
        [-85.47, 43.95], [-85.53, 44.05],
    ]
    features = []
    for rank, point in enumerate(points, 1):
        score = float(11 - rank)
        features.append(_feature(f'TARGET-{rank}', {
            'target_id': f'TARGET-{rank}',
            'target_rank': rank,
            'score': score,
            'score_grade': 4.0,
            'score_geology': score - 4.0,
            'open_ground_status': 'not_applicable',
            'open_ground_value': None,
            'open_ground_display': 'N/A',
            'surface_record_id': 'SURF-1',
            'mineral_interest_id': 'MIN-1',
            'surface_class': 'state',
            'mineral_class': 'state',
            'approach': 'state_lease',
            'source_id': 'ws11_fixture_ranking',
            'name': f'Fixture target {rank}',
        }, {'type': 'Point', 'coordinates': point}))
    return {
        'schema_version': 1,
        'state': code,
        'kind': 'ranked_targets',
        'source_ids': ['ws11_fixture_ranking'],
        'official_source_urls': ['https://example.gov/ranked-targets'],
        'retrieved': '2026-08-13',
        'complete': True,
        'truncated': False,
        'pagination': _pagination(len(features)),
        'type': 'FeatureCollection',
        'features': features,
        'method_id': 'ws11-target-score-v1',
        'input_sha256s': {
            'grades': 'a' * 64,
            'geology': 'b' * 64,
            'land_context': context_sha,
        },
        'top_target_count': 5,
    }


def _write_json(path, value):
    raw = json.dumps(
        value, separators=(',', ':'), ensure_ascii=False,
        allow_nan=False).encode('utf-8')
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'wb') as output:
        output.write(raw)
    return raw


class Fixture:
    def __init__(self, directory):
        self.base = directory
        self.staging = os.path.join(directory, 'private-land-context')
        self.inventory_path = os.path.join(self.staging, 'inventory.json')
        self.publish = os.path.join(directory, 'publish')
        self.latest = os.path.join(self.publish, 'latest.json')
        os.makedirs(self.staging)
        self.snapshots = {}
        states = {}
        for code in sorted(landpub.NON_CLAIM_STATES):
            # Only MI is normalized in focused builds. All 90 files are still
            # present and checksum-verified to exercise national scope.
            surface = _surface_snapshot(code)
            mineral = _mineral_snapshot(code)
            surface_raw = self._save_snapshot(code, 'surface_ownership', surface)
            mineral_raw = self._save_snapshot(code, 'mineral_interests', mineral)
            context_sha = landpub._context_input_sha256(
                hashlib.sha256(surface_raw).hexdigest(),
                hashlib.sha256(mineral_raw).hexdigest())
            ranked = _ranked_snapshot(context_sha, code)
            ranked_raw = self._save_snapshot(code, 'ranked_targets', ranked)
            states[code] = {}
            for kind, raw in (
                    ('ranked_targets', ranked_raw),
                    ('surface_ownership', surface_raw),
                    ('mineral_interests', mineral_raw)):
                states[code][kind] = {
                    'file': landpub._snapshot_filename(code, kind),
                    'n': len(self.snapshots[(code, kind)]['features']),
                    'bytes': len(raw),
                    'sha256': hashlib.sha256(raw).hexdigest(),
                }
        self.inventory = {
            'schema_version': 1,
            'system': landpub.SYSTEM,
            'created': '2026-08-13',
            'clip': {
                'authority': landpub.CLIP_AUTHORITY,
                'method': landpub.CLIP_METHOD,
                'artifact_sha256': landpub._sha256_file(
                    landpub.DEFAULT_STATE_CLIPS),
            },
            'states': states,
        }
        self.save_inventory()

    def _save_snapshot(self, code, kind, value):
        self.snapshots[(code, kind)] = copy.deepcopy(value)
        return _write_json(
            os.path.join(self.staging, landpub._snapshot_filename(code, kind)),
            value)

    def save_inventory(self):
        _write_json(self.inventory_path, self.inventory)

    def rewrite(self, code, kind, mutate):
        value = copy.deepcopy(self.snapshots[(code, kind)])
        mutate(value)
        raw = self._save_snapshot(code, kind, value)
        entry = self.inventory['states'][code][kind]
        entry.update({
            'n': len(value['features']),
            'bytes': len(raw),
            'sha256': hashlib.sha256(raw).hexdigest(),
        })
        self.save_inventory()

    def rebind_ranked_context(self, code='MI'):
        surface_sha = self.inventory['states'][code][
            'surface_ownership']['sha256']
        mineral_sha = self.inventory['states'][code][
            'mineral_interests']['sha256']
        expected = landpub._context_input_sha256(surface_sha, mineral_sha)
        self.rewrite(
            code, 'ranked_targets',
            lambda value: value['input_sha256s'].__setitem__(
                'land_context', expected))

    def load(self):
        return landpub.load_inventory(
            self.staging, self.inventory_path, landpub.DEFAULT_STATE_CLIPS)

    def normalize(self, code='MI'):
        return landpub._normalize_state(self.load(), code)


class NationalLandContextTests(unittest.TestCase):
    def test_exact_30_scope_and_two_layer_normalization(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(directory)
            context = fixture.load()
            normalized = landpub._normalize_state(context, 'MI')
        self.assertEqual(len(context['codes']), 30)
        self.assertEqual(set(context['codes']), set(landpub.NON_CLAIM_STATES))
        self.assertEqual(len(context['paths']), 90)
        self.assertEqual(len(normalized['context_features']), 1)
        self.assertEqual(len(normalized['target_features']), 5)
        land = normalized['context_features'][0]['properties']
        target = normalized['target_features'][0]['properties']
        self.assertEqual(land['mineral_class'], 'state')
        self.assertEqual(land['approach'], 'state_lease')
        self.assertEqual(target['open_ground_status'], 'not_applicable')
        self.assertEqual(target['open_ground_display'], 'N/A')
        self.assertNotIn('open_ground_value', target)
        self.assertNotIn('open_ground_score', target)
        self.assertEqual(target['score'],
                         target['score_grade'] + target['score_geology'])

    def test_inventory_is_exact_checksummed_and_private(self):
        mutations = [
            lambda value: value['states'].pop('AL'),
            lambda value: value['states'].__setitem__(
                'NV', copy.deepcopy(value['states']['AL'])),
            lambda value: value['states']['AL'].pop('mineral_interests'),
        ]
        for mutate in mutations:
            with self.subTest(mutate=mutate), tempfile.TemporaryDirectory() as directory:
                fixture = Fixture(directory)
                mutate(fixture.inventory)
                fixture.save_inventory()
                with self.assertRaises(landpub.PublicationError):
                    fixture.load()
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(directory)
            path = os.path.join(fixture.staging, 'al_ranked_targets.json')
            with open(path, 'ab') as output:
                output.write(b' ')
            with self.assertRaisesRegex(landpub.PublicationError, 'sha256'):
                fixture.load()
        with tempfile.TemporaryDirectory() as directory:
            fake_site = os.path.join(directory, 'site')
            os.makedirs(fake_site)
            fixture = Fixture(fake_site)
            with mock.patch.object(landpub, 'SITE', fake_site), self.assertRaisesRegex(
                    landpub.PublicationError, 'outside site'):
                fixture.load()

    def test_caps_truncation_and_mutation_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(directory)
            fixture.rewrite(
                'MI', 'surface_ownership',
                lambda value: value.__setitem__('truncated', True))
            fixture.rebind_ranked_context()
            with self.assertRaisesRegex(landpub.PublicationError, 'completeness'):
                fixture.normalize()
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(directory)
            fixture.rewrite(
                'MI', 'surface_ownership',
                lambda value: value['pagination'].__setitem__(
                    'pagination_exhausted', False))
            fixture.rebind_ranked_context()
            with self.assertRaisesRegex(landpub.PublicationError,
                                        'pagination_exhausted'):
                fixture.normalize()
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(directory)
            context = fixture.load()
            with open(context['paths'][('AL', 'ranked_targets')], 'ab') as output:
                output.write(b'changed')
            with self.assertRaisesRegex(landpub.PublicationError,
                                        'changed during build'):
                landpub.assert_inputs_unchanged(context)
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(directory)
            context = fixture.load()
            with mock.patch.object(
                    landpub, '_registry_generation_sha256',
                    return_value='f' * 64), self.assertRaisesRegex(
                        landpub.PublicationError, 'registry changed'):
                landpub.assert_inputs_unchanged(context)

    def test_open_ground_na_cannot_be_replaced_with_numeric_zero(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(directory)
            fixture.rewrite(
                'MI', 'ranked_targets',
                lambda value: value['features'][0]['properties'].__setitem__(
                    'open_ground_value', 0))
            with self.assertRaisesRegex(landpub.PublicationError,
                                        'never numeric zero'):
                fixture.normalize()

    def test_minerals_are_independent_and_never_inferred_from_surface(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(directory)
            def make_unknown(value):
                props = value['features'][0]['properties']
                props['mineral_class'] = 'unknown'
                props['confidence'] = 'unknown'
                props['evidence_basis'] = 'unresolved'
                props['note'] = ('Independent mineral-title review was '
                                 'inconclusive; surface management is not title.')
            fixture.rewrite('MI', 'mineral_interests', make_unknown)
            fixture.rebind_ranked_context()
            def update_target(value):
                for feature in value['features']:
                    feature['properties']['mineral_class'] = 'unknown'
                    feature['properties']['approach'] = 'research_only'
            fixture.rewrite('MI', 'ranked_targets', update_target)
            normalized = fixture.normalize()
            land = normalized['context_features'][0]['properties']
            self.assertEqual(land['surface_class'], 'state')
            self.assertEqual(land['mineral_class'], 'unknown')
            self.assertEqual(land['approach'], 'research_only')
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(directory)
            fixture.rewrite(
                'MI', 'mineral_interests',
                lambda value: value['features'][0]['properties'].__setitem__(
                    'evidence_basis', 'surface_management'))
            fixture.rebind_ranked_context()
            with self.assertRaisesRegex(landpub.PublicationError,
                                        'never surface management'):
                fixture.normalize()

    def test_target_join_must_be_exact_and_unambiguous(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(directory)
            def overlap(value):
                duplicate = copy.deepcopy(value['features'][0])
                duplicate['id'] = 'SURF-2'
                duplicate['properties']['record_id'] = 'SURF-2'
                value['features'].append(duplicate)
                value['pagination'] = _pagination(2)
            fixture.rewrite('MI', 'surface_ownership', overlap)
            fixture.rebind_ranked_context()
            with self.assertRaisesRegex(landpub.PublicationError,
                                        'does not join exactly'):
                fixture.normalize()
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(directory)
            fixture.rewrite(
                'MI', 'ranked_targets',
                lambda value: value['features'][0]['properties'].__setitem__(
                    'mineral_interest_id', 'MISSING'))
            with self.assertRaisesRegex(landpub.PublicationError,
                                        'declared mineral record'):
                fixture.normalize()

    def test_state_clip_and_prepartitioned_mineral_geometry_are_required(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(directory)
            fixture.rewrite(
                'MI', 'surface_ownership',
                lambda value: value['features'][0].__setitem__(
                    'geometry', _square(-120, 35, 0.1)))
            fixture.rebind_ranked_context()
            with self.assertRaisesRegex(landpub.PublicationError,
                                        'outside authoritative MI'):
                fixture.normalize()
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(directory)
            fixture.rewrite(
                'MI', 'mineral_interests',
                lambda value: value['features'][0].__setitem__(
                    'geometry', _square(MI_POINT[0], MI_POINT[1], 0.1)))
            fixture.rebind_ranked_context()
            with self.assertRaisesRegex(landpub.PublicationError,
                                        'not pre-partitioned'):
                fixture.normalize()

    def test_polygon_containment_does_not_silently_fill_interior_holes(self):
        container = {
            'type': 'Polygon',
            'coordinates': [
                _square(0, 0, 2)['coordinates'][0],
                _square(0, 0, 0.5)['coordinates'][0],
            ],
        }
        fills_hole = _square(0, 0, 1)
        preserves_hole = {
            'type': 'Polygon',
            'coordinates': [
                _square(0, 0, 1)['coordinates'][0],
                _square(0, 0, 0.5)['coordinates'][0],
            ],
        }
        self.assertFalse(landpub._geometry_contains_geometry(
            container, fills_hole))
        self.assertTrue(landpub._geometry_contains_geometry(
            container, preserves_hole))

    @unittest.skipUnless(shutil.which('tippecanoe'), 'tippecanoe is required')
    def test_real_pmtiles_full_scan_and_immutable_generation_pointer(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(directory)
            registry_before = landpub._sha256_file(
                os.path.join(ROOT, 'states', 'MI.yaml'))
            manifest_before = landpub._sha256_file(
                os.path.join(ROOT, 'site', 'data', 'manifest.json'))
            result = landpub.build(
                fixture.staging, fixture.inventory_path, fixture.publish,
                latest_manifest=fixture.latest, states=['MI'])
            self.assertEqual(result['states'], ['MI'])
            latest, _ = landpub._read_strict_json(fixture.latest)
            pointer = latest['artifacts']['MI']
            generation_path = os.path.join(
                os.path.dirname(fixture.latest), pointer['generation_file'])
            generation, generation_raw = landpub._read_strict_json(generation_path)
            archive_path = os.path.join(
                os.path.dirname(fixture.latest), pointer['artifact_file'])
            self.assertEqual(hashlib.sha256(generation_raw).hexdigest(),
                             pointer['generation_sha256'])
            self.assertEqual(landpub._sha256_file(archive_path),
                             pointer['artifact_sha256'])
            self.assertEqual(pointer['registry_sha256'],
                             generation['registry_sha256'])
            self.assertEqual(generation['publication']['source_layer_counts'], {
                'land_context': 1, 'target_context': 5})
            self.assertEqual(
                generation['scoring']['open_ground'],
                {'display': 'N/A', 'status': 'not_applicable', 'value': None})
            normalized = fixture.normalize()
            checked = landpub.validate_pmtiles(
                archive_path, 'MI', normalized,
                landpub._state_bounds(fixture.load(), 'MI'))
            self.assertGreater(checked['decoded_features']['land_context'], 0)
            self.assertGreater(checked['decoded_features']['target_context'], 0)
            self.assertIn(pointer['artifact_sha256'],
                          os.path.basename(archive_path))
            self.assertIn(pointer['generation_sha256'],
                          os.path.basename(generation_path))
            self.assertEqual(registry_before, landpub._sha256_file(
                os.path.join(ROOT, 'states', 'MI.yaml')))
            self.assertEqual(manifest_before, landpub._sha256_file(
                os.path.join(ROOT, 'site', 'data', 'manifest.json')))

            # A semantic expectation mutation is detected by the full scan.
            bad = copy.deepcopy(normalized)
            bad['targets']['TARGET-1']['score'] += 1
            with self.assertRaisesRegex(landpub.PublicationError, 'changed'):
                landpub.validate_pmtiles(
                    archive_path, 'MI', bad,
                    landpub._state_bounds(fixture.load(), 'MI'))

    def test_claim_state_filter_and_missing_tiler_do_not_publish(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(directory)
            with self.assertRaisesRegex(landpub.PublicationError,
                                        'claim/unsupported'):
                landpub.build(
                    fixture.staging, fixture.inventory_path, fixture.publish,
                    states=['NV'])
            self.assertFalse(os.path.exists(fixture.latest))
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(directory)
            with self.assertRaisesRegex(landpub.PublicationError, 'tippecanoe'):
                landpub.build(
                    fixture.staging, fixture.inventory_path, fixture.publish,
                    states=['MI'], tippecanoe='definitely-not-a-command')
            self.assertFalse(os.path.exists(fixture.latest))


if __name__ == '__main__':
    unittest.main()
