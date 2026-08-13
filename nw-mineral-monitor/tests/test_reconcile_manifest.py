import copy
import os
import sys
import unittest
from unittest import mock


ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.join(ROOT, 'pipelines'))

import reconcile_manifest
import state_registry


def _vector(url, source_layers, **extra):
    value = {
        'browser_delivery': 'pmtiles',
        'artifact': url,
        'source_layers': list(source_layers),
    }
    value.update(extra)
    return value


def _aeromag(code):
    code = code.lower()
    return {
        'browser_delivery': 'cog',
        'artifact': f'map-assets/releases/{code}/aeromag-abc.tif',
        'sha256': 'a' * 64,
        'bytes': 4096,
        'tile_url': f'/map-assets/releases/{code}/aeromag/{{z}}/{{x}}/{{y}}.webp',
        'tile_size': 256,
        'minzoom': 4,
        'maxzoom': 12,
        'bounds': [-120, 30, -110, 45],
    }


def _state(code='NV', regime='claim'):
    shared_claims = 'map-assets/releases/national/federal-abc.pmtiles'
    systems = []
    if regime == 'claim':
        systems.append(_vector(
            shared_claims, ['active', 'closed', 'open_ground'],
            id='federal_mlrs',
            layer_metadata={
                'active': {'n': 12, 'availability': 'complete', 'complete': True},
                'closed': {'n': 30, 'availability': 'complete', 'complete': True},
                'open_ground': {'n': 8, 'availability': 'complete', 'complete': True},
            }))
    if code == 'AK':
        systems.append(_vector(
            'map-assets/releases/ak/state-claims-abc.pmtiles',
            ['active', 'pending', 'closed'], id='alaska_state_claims',
            layer_counts={'active': 4, 'pending': 1, 'closed': 6},
            layer_availability={
                'active': 'complete', 'pending': 'complete', 'closed': 'complete'}))
    return {
        'release': {'enabled': True, 'status': 'done'},
        'regime': regime,
        'query_envelopes': ([
            {'id': 'west', 'bbox': [-180, 50, -129, 72], 'crs': 'EPSG:4326'},
            {'id': 'east', 'bbox': [172, 50, 180, 55], 'crs': 'EPSG:4326'},
        ] if code == 'AK' else [
            {'id': 'statewide', 'bbox': [-120, 30, -110, 45],
             'crs': 'EPSG:4326'},
        ]),
        'geology': _vector(
            'map-assets/releases/national/geology-abc.pmtiles',
            ['units-v2', 'geologic-overlays']),
        'faults': _vector(
            'map-assets/releases/national/faults-abc.pmtiles', ['fault-lines-v3']),
        'land_context': _vector(
            f'map-assets/releases/{code.lower()}/land-abc.pmtiles',
            ['land_context', 'target_context'],
            layer_metadata={
                'land_context': {'n': 17, 'availability': 'complete', 'complete': True},
                'target_context': {'n': 5, 'availability': 'complete', 'complete': True},
            }),
        'claim_systems': systems,
        'aeromag': _aeromag(code),
    }


class ReleaseDescriptorTests(unittest.TestCase):
    def test_compiles_registry_layers_geometry_counts_and_state_filters(self):
        with mock.patch.object(
                reconcile_manifest, 'load_states',
                return_value={'AK': _state('AK'), 'NV': _state('NV')}):
            layers = reconcile_manifest.tiled_layers()

        nv = [item for item in layers if item['state'] == 'NV']
        self.assertEqual(
            {item['source_layer'] for item in nv if item['kind'] == 'geology'},
            {'units-v2', 'geologic-overlays'})
        self.assertEqual(
            {item['source_layer'] for item in nv if item['kind'] == 'faults'},
            {'fault-lines-v3'})
        federal = [item for item in layers
                   if item.get('system') == 'federal_mlrs']
        self.assertTrue(federal)
        for item in federal:
            expected_type = ('fill' if item['source_layer'] == 'open_ground'
                             else 'circle')
            self.assertEqual(item['style_layers'][0]['type'], expected_type)
            expected_filter = ['==', ['get', 'st'], item['state']]
            self.assertEqual(item['filter'], expected_filter)
            self.assertEqual(item['style_layers'][0]['filter'], expected_filter)
            self.assertIsInstance(item['n'], int)
            self.assertEqual(item['availability'], 'complete')
            self.assertTrue(item['complete'])
            self.assertEqual(item['activation_minzoom'], 4)
            self.assertTrue(item['view_bounds'])

        dnr = [item for item in layers
               if item.get('system') == 'alaska_state_claims']
        self.assertEqual({item['source_layer'] for item in dnr},
                         {'active', 'pending', 'closed'})
        self.assertTrue(all(item['style_layers'][0]['type'] == 'fill'
                            for item in dnr))
        self.assertEqual({item['n'] for item in dnr}, {1, 4, 6})
        self.assertTrue(all(item['availability'] == 'complete' for item in dnr))
        self.assertTrue(all(len(item['view_bounds']) == 2 for item in dnr))

    def test_archive_url_has_one_shared_source_identity(self):
        with mock.patch.object(
                reconcile_manifest, 'load_states',
                return_value={'AK': _state('AK'), 'NV': _state('NV')}):
            layers = reconcile_manifest.tiled_layers()
        by_url = {}
        for item in layers:
            if item['delivery'] == 'pmtiles':
                by_url.setdefault(item['url'], set()).add(item['source_id'])
        self.assertTrue(any(len([item for item in layers if item.get('url') == url]) > 1
                            for url in by_url))
        self.assertTrue(all(len(source_ids) == 1
                            for source_ids in by_url.values()))
        shared_claims = [item for item in layers
                         if item.get('url', '').endswith('federal-abc.pmtiles')]
        self.assertEqual({item['state'] for item in shared_claims}, {'AK', 'NV'})
        self.assertEqual(len({item['source_id'] for item in shared_claims}), 1)

    def test_federal_claims_and_open_ground_may_use_separate_archives(self):
        state = _state('NV')
        federal = state['claim_systems'][0]
        federal['publication_artifacts'] = {
            'claims': _vector(
                'map-assets/releases/nv/claims.pmtiles', ['active', 'closed'],
                layer_metadata={key: federal['layer_metadata'][key]
                                for key in ('active', 'closed')}),
            'open_ground': _vector(
                'map-assets/releases/nv/open-ground.pmtiles', ['open_ground'],
                layer_metadata={'open_ground': federal['layer_metadata']['open_ground']}),
        }
        with mock.patch.object(reconcile_manifest, 'load_states',
                               return_value={'NV': state}):
            layers = [item for item in reconcile_manifest.tiled_layers()
                      if item.get('system') == 'federal_mlrs']
        by_layer = {item['source_layer']: item for item in layers}
        self.assertNotEqual(by_layer['active']['source_id'],
                            by_layer['open_ground']['source_id'])
        self.assertEqual(by_layer['active']['source_id'],
                         by_layer['closed']['source_id'])
        self.assertEqual(by_layer['open_ground']['style_layers'][0]['type'], 'fill')

    def test_nonclaim_uses_registry_land_context_layer(self):
        with mock.patch.object(
                reconcile_manifest, 'load_states',
                return_value={'NY': _state('NY', 'non_claim')}):
            layers = reconcile_manifest.tiled_layers()
        context = [item for item in layers if item['kind'] == 'land-context']
        self.assertEqual(len(context), 2)
        by_layer = {item['source_layer']: item for item in context}
        self.assertEqual(by_layer['land_context']['style_layers'][0]['type'], 'fill')
        self.assertEqual(by_layer['target_context']['style_layers'][0]['type'], 'circle')
        self.assertEqual(by_layer['target_context']['n'], 5)
        self.assertEqual(by_layer['target_context']['activation_minzoom'], 4)
        self.assertFalse(any(item.get('system') for item in layers))

    def test_nonclaim_compiles_only_accepted_aml_and_trust_layers(self):
        state = _state('MI', 'non_claim')
        state['aml'] = _vector(
            'map-assets/releases/mi/aml.pmtiles', ['aml'],
            release_inventory_status='ingested_complete',
            layer_metadata={
                'aml': {'n': 12, 'availability': 'complete', 'complete': True}})
        state['trust_land'] = _vector(
            'map-assets/releases/mi/trust.pmtiles', ['trust_land'],
            release_inventory_status='ingested_complete',
            layer_metadata={
                'trust_land': {'n': 7, 'availability': 'complete', 'complete': True}})
        with mock.patch.object(reconcile_manifest, 'load_states',
                               return_value={'MI': state}):
            layers = reconcile_manifest.tiled_layers()
        aml = next(item for item in layers if item['kind'] == 'aml')
        trust = next(item for item in layers if item['kind'] == 'trust-land')
        self.assertEqual(aml['style_layers'][0]['type'], 'circle')
        self.assertEqual(trust['style_layers'][0]['type'], 'fill')
        self.assertEqual((aml['n'], trust['n']), (12, 7))

    def test_raster_descriptor_uses_xyz_and_retains_cog_identity(self):
        state = _state('NV')
        with mock.patch.object(reconcile_manifest, 'load_states',
                               return_value={'NV': state}):
            descriptor = next(item for item in reconcile_manifest.tiled_layers()
                              if item['kind'] == 'aeromag')
        self.assertEqual(descriptor['delivery'], 'cog')
        self.assertEqual(descriptor['url'], descriptor['tile_url_template'])
        self.assertIn('{z}/{x}/{y}', descriptor['url'])
        self.assertFalse(descriptor['url'].endswith('.tif'))
        self.assertEqual(descriptor['tile_scheme'], 'xyz')
        self.assertEqual(descriptor['bounds'], state['aeromag']['bounds'])
        self.assertEqual(descriptor['minzoom'], state['aeromag']['minzoom'])
        self.assertEqual(descriptor['maxzoom'], state['aeromag']['maxzoom'])
        self.assertEqual(descriptor['cog'], {
            'url': state['aeromag']['artifact'],
            'sha256': state['aeromag']['sha256'],
            'bytes': state['aeromag']['bytes'],
        })

    def test_direct_tiff_or_incomplete_xyz_fails_descriptor(self):
        for tile_url in (
                'map-assets/releases/nv/aeromag.tif',
                '/tiles/aeromag/{z}/{x}.webp'):
            with self.subTest(tile_url=tile_url):
                state = _state('NV')
                state['aeromag']['tile_url'] = tile_url
                with mock.patch.object(reconcile_manifest, 'load_states',
                                       return_value={'NV': state}), self.assertRaises(
                                           ValueError):
                    reconcile_manifest.tiled_layers()

    def test_raster_descriptor_requires_lifecycle_bounds_and_zooms(self):
        for missing in ('bounds', 'minzoom', 'maxzoom'):
            with self.subTest(missing=missing):
                state = _state('NV')
                state['aeromag'].pop(missing)
                with mock.patch.object(reconcile_manifest, 'load_states',
                                       return_value={'NV': state}), self.assertRaises(
                                           ValueError):
                    reconcile_manifest.tiled_layers()

    def test_missing_vector_source_layers_fails_instead_of_inventing_names(self):
        state = _state('NV')
        del state['geology']['source_layers']
        with mock.patch.object(reconcile_manifest, 'load_states',
                               return_value={'NV': state}), self.assertRaisesRegex(
                                   ValueError, 'source_layers'):
            reconcile_manifest.tiled_layers()

    def test_vector_descriptor_requires_state_lifecycle_bounds(self):
        state = _state('NV')
        state['query_envelopes'] = []
        with mock.patch.object(reconcile_manifest, 'load_states',
                               return_value={'NV': state}), self.assertRaisesRegex(
                                   ValueError, 'query-envelope bounds'):
            reconcile_manifest.tiled_layers()

    def test_slug_collisions_fail_instead_of_overwriting_map_layers(self):
        state = _state('NV')
        state['geology']['source_layers'] = ['units-v2', 'units_v2']
        with mock.patch.object(reconcile_manifest, 'load_states',
                               return_value={'NV': state}), self.assertRaisesRegex(
                                   ValueError, 'duplicate release descriptor id'):
            reconcile_manifest.tiled_layers()

    def test_shared_source_rejects_conflicting_promote_id(self):
        rows = [
            {'id': 'a', 'source_id': 'same', 'url': 'a.pmtiles',
             'delivery': 'pmtiles', 'promote_id': 'fid',
             'view_bounds': [[-120, 30, -110, 45]],
             'activation_minzoom': 4, 'style_layers': []},
            {'id': 'b', 'source_id': 'same', 'url': 'a.pmtiles',
             'delivery': 'pmtiles', 'promote_id': 'objectid',
             'view_bounds': [[-120, 30, -110, 45]],
             'activation_minzoom': 4, 'style_layers': []},
        ]
        with self.assertRaisesRegex(ValueError, 'multiple promote_id'):
            reconcile_manifest._validate_compiled_descriptors(rows)


class RasterRegistryContractTests(unittest.TestCase):
    def test_xyz_template_contract(self):
        self.assertTrue(state_registry.is_xyz_tile_template(
            'https://tiles.example.test/a/{z}/{x}/{y}.png?key=public'))
        self.assertTrue(state_registry.is_xyz_tile_template(
            '/tiles/a/{z}/{x}/{y}.webp'))
        for bad in (
                'data/cog/a.tif', '/tiles/{z}/{x}.png',
                'javascript:alert(1)/{z}/{x}/{y}', '//tiles/{z}/{x}/{y}.png',
                '/tiles/../private/{z}/{x}/{y}.png'):
            with self.subTest(bad=bad):
                self.assertFalse(state_registry.is_xyz_tile_template(bad))

    def test_release_delivery_rejects_tiff_as_browser_tiles(self):
        raster = {
            'browser_delivery': 'cog',
            'artifact': f"map-assets/releases/nv/{'a' * 64}.tif",
            'sha256': 'a' * 64,
            'bytes': 4096,
            'tile_url': 'map-assets/releases/nv/aeromag.tif',
            'bounds': [-120, 35, -114, 42],
            'minzoom': 4,
            'maxzoom': 12,
        }
        errors = []
        state_registry._validate_delivery(
            raster, 'NV aeromag', errors, raster=True, release=True)
        self.assertTrue(any('valid XYZ' in error for error in errors))

        raster['tile_url'] = '/tiles/nv/aeromag/{z}/{x}/{y}.webp'
        errors = []
        state_registry._validate_delivery(
            raster, 'NV aeromag', errors, raster=True, release=True)
        self.assertEqual(errors, [])


class AuditedPointBaselineTests(unittest.TestCase):
    def _manifest(self):
        return {
            'national_baselines': {
                baseline_id: copy.deepcopy(values['fingerprint'])
                for baseline_id, values in
                reconcile_manifest.AUDITED_POINT_BASELINES.items()
            },
        }

    def test_exact_artifact_fingerprint_receives_audited_inventory(self):
        manifest = self._manifest()
        reconcile_manifest.reconcile_audited_point_baselines(manifest)
        for baseline_id, audited in (
                reconcile_manifest.AUDITED_POINT_BASELINES.items()):
            self.assertEqual(
                manifest['national_baselines'][baseline_id]['source_id_inventory'],
                audited['source_id_inventory'])

    def test_changed_artifact_cannot_inherit_stale_inventory(self):
        manifest = self._manifest()
        reconcile_manifest.reconcile_audited_point_baselines(manifest)
        manifest['national_baselines']['mrds']['sha256'] = '0' * 64
        with self.assertRaisesRegex(ValueError, 'stale audited'):
            reconcile_manifest.reconcile_audited_point_baselines(manifest)

    def test_future_fingerprint_with_distinct_inventory_is_preserved(self):
        manifest = self._manifest()
        future = {'status': 'complete_at_retrieval', 'source_records': 1,
                  'maxzoom_feature_instances': 1,
                  'maxzoom_unique_tiled_ids': 1,
                  'ids_sha256': 'f' * 64}
        manifest['national_baselines']['usmin']['sha256'] = '1' * 64
        manifest['national_baselines']['usmin']['source_id_inventory'] = future
        reconcile_manifest.reconcile_audited_point_baselines(manifest)
        self.assertEqual(
            manifest['national_baselines']['usmin']['source_id_inventory'], future)


if __name__ == '__main__':
    unittest.main()
