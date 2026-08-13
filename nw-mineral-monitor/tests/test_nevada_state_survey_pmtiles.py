import importlib
import json
import os
import shutil
import sys
import tempfile
import types
import unittest
import zipfile
from unittest import mock


ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), '..'))
PIPELINES = os.path.join(ROOT, 'pipelines')
sys.path.insert(0, PIPELINES)

nv = importlib.import_module('build_nevada_state_survey_pmtiles')
national = importlib.import_module('validate_national')


POLYGON = {
    'type': 'Polygon',
    'coordinates': [[[-119.0, 40.0], [-118.9, 40.0],
                     [-118.9, 40.1], [-119.0, 40.0]]],
}


def arc_feature(oid, properties=None):
    values = {'OBJECTID': oid}
    values.update(properties or {})
    return {'type': 'Feature', 'properties': values, 'geometry': POLYGON}


class NevadaStateSurveyTests(unittest.TestCase):
    def test_contract_is_tiled_only_and_not_a_release_path(self):
        self.assertEqual(set(nv.BASELINE_KEYS), {
            'nv_usgs_ds249', 'nv_nbmg_onegeology_250k',
            'nv_nbmg_mining_districts'})
        self.assertTrue(all(path.endswith('.pmtiles')
                            for path in nv.BASELINE_KEYS.values()))
        self.assertTrue(all('/tiles/states/nv/' in path
                            for path in nv.BASELINE_KEYS.values()))
        self.assertNotIn('release', '/'.join(nv.BASELINE_KEYS.values()))
        self.assertNotEqual(
            os.path.commonpath((os.path.realpath(nv.SITE),
                                os.path.realpath(nv.PRIVATE_STAGING_ROOT))),
            os.path.realpath(nv.SITE))
        self.assertNotIn('/site/', nv.PRIVATE_STAGING_ROOT)
        self.assertEqual(nv.DS249_BYTES, 183_472_749)
        self.assertRegex(nv.DS249_SHA256, r'^[0-9a-f]{64}$')

    def test_tippecanoe_version_accepts_tool_stderr_contract(self):
        completed = types.SimpleNamespace(
            stdout='', stderr='tippecanoe v2.79.0\n')
        with mock.patch.object(nv.subprocess, 'run', return_value=completed):
            self.assertEqual(nv._tippecanoe_version(), 'v2.79.0')

    def test_public_staging_misconfiguration_is_rejected_before_mkdir(self):
        with tempfile.TemporaryDirectory() as directory:
            site = os.path.join(directory, 'site')
            bad_staging = os.path.join(site, 'data', '.staging')
            with mock.patch.object(nv, 'SITE', site), \
                    mock.patch.object(nv, 'PRIVATE_STAGING_ROOT', bad_staging), \
                    mock.patch.object(nv.os, 'makedirs') as makedirs:
                with self.assertRaisesRegex(RuntimeError, 'outside public site'):
                    nv._ensure_private_staging_root()
            makedirs.assert_not_called()
            self.assertFalse(os.path.exists(bad_staging))

    def test_layer_snapshot_pins_unique_ids_and_metadata_hash(self):
        metadata = {
            'name': 'US-NV_NBMG_250k_Geology',
            'geometryType': 'esriGeometryPolygon',
            'description': 'Geology at 1:250,000 scale.',
            'maxRecordCount': 1000,
            'fields': [{'name': 'OBJECTID', 'type': 'esriFieldTypeOID'}],
        }
        ids = {'objectIdFieldName': 'OBJECTID', 'objectIds': [3, 1, 2]}
        with mock.patch.object(nv, '_request_json', side_effect=[metadata, ids]):
            got = nv._layer_snapshot(
                'https://example.test/23', 'US-NV_NBMG_250k_Geology',
                'esriGeometryPolygon', '1:250,000')
        self.assertEqual(got['ids'], [1, 2, 3])
        self.assertRegex(got['id_sha256'], r'^[0-9a-f]{64}$')
        self.assertRegex(got['metadata_sha256'], r'^[0-9a-f]{64}$')

        duplicate = {'objectIdFieldName': 'OBJECTID', 'objectIds': [1, 1]}
        with mock.patch.object(nv, '_request_json', side_effect=[metadata, duplicate]):
            with self.assertRaisesRegex(RuntimeError, 'empty or duplicated'):
                nv._layer_snapshot(
                    'https://example.test/23', 'US-NV_NBMG_250k_Geology',
                    'esriGeometryPolygon', '1:250,000')

    def test_reviewed_arcgis_snapshot_contract_fails_on_live_drift(self):
        contract = nv.ARCGIS_SNAPSHOT_CONTRACTS[
            'nv_nbmg_mining_districts']
        snapshot = {
            'oid_field': contract['object_id_field'],
            'ids': list(range(contract['minimum_object_id'],
                              contract['maximum_object_id'] + 1)),
            'id_sha256': contract['object_ids_sha256'],
            'metadata_sha256': contract['layer_metadata_sha256'],
        }
        nv._assert_arcgis_snapshot_contract(
            'nv_nbmg_mining_districts', snapshot)
        snapshot['ids'].append(536)
        with self.assertRaisesRegex(RuntimeError, 'snapshot changed'):
            nv._assert_arcgis_snapshot_contract(
                'nv_nbmg_mining_districts', snapshot)

    def test_national_progress_hook_requires_and_exactly_validates_atomic_set(self):
        keys = set(national._NEVADA_STATE_SURVEY_BASELINES)
        qa = national.QA()
        national.validate_nevada_state_survey_baselines(
            qa, {next(iter(keys)): {}})
        self.assertEqual(len(qa.errors), 1)
        self.assertIn('atomic three-archive set', qa.errors[0])

        baselines = {key: {} for key in keys}
        qa = national.QA()
        with mock.patch.object(nv, 'validate_manifest_baselines') as exact:
            national.validate_nevada_state_survey_baselines(qa, baselines)
        self.assertEqual(qa.errors, [])
        exact.assert_called_once_with(
            {'national_baselines': baselines},
            pmtiles_header=national._pmtiles_header)

        qa = national.QA()
        with mock.patch.object(
                nv, 'validate_manifest_baselines',
                side_effect=RuntimeError('semantic drift')):
            national.validate_nevada_state_survey_baselines(qa, baselines)
        self.assertEqual(len(qa.errors), 1)
        self.assertIn('semantic drift', qa.errors[0])

    def test_arcgis_pages_reconcile_exactly_to_snapshot(self):
        snapshot = {'oid_field': 'OBJECTID', 'ids': [1, 2, 3]}
        pages = [
            {'features': [arc_feature(1), arc_feature(2)]},
            {'features': [arc_feature(3)]},
        ]
        with mock.patch.object(nv, '_request_json', side_effect=pages) as request:
            got = list(nv._iter_snapshot(
                'https://example.test/23', snapshot, ('OBJECTID',), page=2))
        self.assertEqual([row['properties']['OBJECTID'] for row in got], [1, 2, 3])
        self.assertEqual(request.call_args_list[0].args[1]['objectIds'], '1,2')
        self.assertTrue(request.call_args_list[0].kwargs['post'])

        with mock.patch.object(nv, '_request_json', return_value={
                'features': [arc_feature(2), arc_feature(1)]}):
            with self.assertRaisesRegex(RuntimeError, 'snapshot page mismatch'):
                list(nv._iter_snapshot(
                    'https://example.test/23',
                    {'oid_field': 'OBJECTID', 'ids': [1, 2]},
                    ('OBJECTID',), page=2))

    def test_normalizers_stamp_source_and_scale_on_every_polygon(self):
        geology = arc_feature(7, {
            'County': 'Nye', 'identifier': '250k-polygon-7',
            'name': 'Example tuff', 'lithology': 'Tuff',
            'geologicHistory': 'Miocene',
        })
        normalized = nv._normalize_onegeology(geology)
        props = normalized['properties']
        self.assertEqual(props['st'], 'NV')
        self.assertEqual(props['source_scale'], '1:250,000')
        self.assertEqual(props['publication_id'], 'NBMG OneGeology 2013 250k')
        self.assertEqual(props['source_id'], 'nbmg-onegeology-250k:7')

        district = arc_feature(8, {
            'OBJECTID_1': 8, 'District_Name': 'Example district',
            'District_Type': 'metallic', 'District_No': 1234,
        })
        district['properties'].pop('OBJECTID')
        dprops = nv._normalize_district(district)['properties']
        self.assertEqual(dprops['source_scale'], '1:1,000,000')
        self.assertEqual(dprops['district_id'], '1234')
        self.assertEqual(dprops['publication_id'], 'NBMG Report 47z (1998)')

    def test_geometry_rejects_features_outside_nevada(self):
        self.assertEqual(nv._plain_geometry(
            POLYGON, {'Polygon'})['type'], 'Polygon')
        multipart = {
            'type': 'MultiPolygon',
            'coordinates': [POLYGON['coordinates']],
        }
        self.assertEqual(nv._plain_geometry(
            multipart, {'Polygon', 'MultiPolygon'})['type'], 'MultiPolygon')
        outside = json.loads(json.dumps(POLYGON))
        outside['coordinates'][0][0] = [-110, 40]
        with self.assertRaisesRegex(RuntimeError, 'out-of-scope'):
            nv._plain_geometry(outside, {'Polygon'})

    def test_arcgis_empty_geometry_is_explicitly_inventoried(self):
        empty = arc_feature(2)
        empty['geometry'] = {'type': 'Polygon', 'coordinates': []}
        valid = arc_feature(3)
        snapshot = {'oid_field': 'OBJECTID', 'ids': [2, 3]}
        with tempfile.TemporaryDirectory() as directory:
            sequence = os.path.join(directory, 'features.geojsonseq')
            with mock.patch.object(nv, '_iter_snapshot', return_value=[empty, valid]):
                stats = nv._stream_arcgis(
                    'https://example.test/23', snapshot, ('OBJECTID',),
                    lambda feature: feature, sequence,
                    expected_empty_geometry_ids=(2,))
            self.assertEqual(stats['source_records'], 2)
            self.assertEqual(stats['n'], 1)
            self.assertEqual(stats['empty_geometry_object_ids'], [2])
            self.assertIsNone(stats['topology_repair'])
            self.assertEqual(stats['spatial_clip']['fully_outside_count'], 0)
            self.assertEqual(stats['spatial_clip']['geometry_clipped_count'], 0)
            self.assertEqual(stats['spatial_clip']['geometry_unchanged_count'], 1)
            with open(sequence, encoding='utf-8') as source:
                self.assertEqual(len(source.readlines()), 1)
            with mock.patch.object(nv, '_iter_snapshot', return_value=[empty, valid]):
                with self.assertRaisesRegex(RuntimeError, 'inventory changed'):
                    nv._stream_arcgis(
                        'https://example.test/23', snapshot, ('OBJECTID',),
                        lambda feature: feature, sequence)

    def test_repair_evidence_pins_ids_versions_types_and_area_ceilings(self):
        record = {
            'object_id': 7, 'absolute_area_delta': 1e-14,
            'relative_area_delta': 2e-14,
        }
        contract = {
            'count': 1,
            'object_ids_sha256': nv._canonical_sha256([7]),
            'validity_reason_counts': {'Ring Self-intersection': 1},
            'type_transition_counts': {'Polygon->Polygon->Polygon': 1},
            'nonpolygon_parts_dropped_by_type': {},
            'shapely_version': '2.0.3', 'geos_version': '3.11.3',
            'max_absolute_area_delta': 1e-12,
            'max_relative_area_delta': 1e-12,
        }
        fake_shapely = types.SimpleNamespace(
            __version__='2.0.3', geos_version_string='3.11.3')
        with mock.patch.object(nv, 'shapely', fake_shapely):
            evidence = nv._repair_evidence(
                [record], {'Ring Self-intersection': 1},
                {'Polygon->Polygon->Polygon': 1}, {}, contract,
                ordering='validate_then_make_valid_then_state_intersection',
                area_units='square degrees in EPSG:4326')
            self.assertEqual(evidence['object_ids'], [7])
            self.assertEqual(
                evidence['ordering'],
                'validate_then_make_valid_then_state_intersection')
            self.assertEqual(
                evidence['area_delta']['maximum_relative']['acceptance_ceiling'],
                1e-12)
            with self.assertRaisesRegex(RuntimeError, 'contract changed'):
                nv._repair_evidence(
                    [dict(record, object_id=8)],
                    {'Ring Self-intersection': 1},
                    {'Polygon->Polygon->Polygon': 1}, {}, contract,
                    ordering='validate_then_make_valid_then_state_intersection',
                    area_units='square degrees in EPSG:4326')
            with self.assertRaisesRegex(RuntimeError, 'exceeds reviewed ceiling'):
                nv._repair_evidence(
                    [dict(record, relative_area_delta=1e-6)],
                    {'Ring Self-intersection': 1},
                    {'Polygon->Polygon->Polygon': 1}, {}, contract,
                    ordering='validate_then_make_valid_then_state_intersection',
                    area_units='square degrees in EPSG:4326')

    def test_ds249_per_record_geometry_and_repair_contracts_are_explicit(self):
        geology = nv.DS249_GEOMETRY_CONTRACT['geology']
        faults = nv.DS249_GEOMETRY_CONTRACT['faults']
        self.assertEqual(geology['source_schema'], 'Polygon')
        self.assertEqual(geology['by_type']['MultiPolygon'], 71)
        self.assertEqual(faults['source_schema'], 'LineString')
        self.assertEqual(faults['by_type']['MultiLineString'], 1)
        self.assertEqual(nv.DS249_GEOLOGY_REPAIR_CONTRACT['count'], 187)
        self.assertEqual(
            nv.DS249_GEOLOGY_REPAIR_CONTRACT['validity_reason_counts'],
            {'Ring Self-intersection': 187})
        self.assertEqual(nv.DS249_FAULT_REPAIR_CONTRACT['count'], 0)
        exclusion = nv.DS249_FAULT_ENCODING_EXCLUSION_CONTRACT
        self.assertEqual(exclusion['count'], 15)
        self.assertEqual(exclusion['reason'],
                         'below_mvt_maxzoom_encoding_resolution')
        self.assertEqual(exclusion['tippecanoe_version'], 'v2.79.0')
        self.assertEqual(exclusion['tippecanoe_maxzoom'], 12)
        self.assertEqual(exclusion['tippecanoe_full_detail'], 12)
        self.assertLessEqual(
            exclusion['maximum_native_length_m'],
            exclusion['maximum_accepted_native_length_m'])
        for contract in (geology, faults):
            self.assertTrue(all(
                len(value) == 64
                for value in contract['by_type_object_ids_sha256'].values()))

    def test_fault_encoding_exclusions_pin_every_source_geometry(self):
        contract = nv.DS249_FAULT_ENCODING_EXCLUSION_CONTRACT
        records = [
            {
                'fid': fid, 'source_record_id': source_id,
                'source_type': 'LineString', 'coordinate_count': 2,
                'native_length_m': length,
                'source_geometry_sha256': geometry_hash,
            }
            for fid, source_id, length, geometry_hash in zip(
                contract['fids'], contract['source_record_ids'],
                contract['native_lengths_m'],
                contract['source_geometry_sha256'])
        ]
        evidence = nv._fault_encoding_exclusion_evidence(records)
        self.assertEqual(evidence['count'], 15)
        self.assertEqual(evidence['reason_code'],
                         'below_mvt_maxzoom_encoding_resolution')
        self.assertEqual(evidence['records_sha256'],
                         contract['records_sha256'])
        nv._validate_manifest_encoding_exclusions(evidence)
        changed = json.loads(json.dumps(records))
        changed[0]['native_length_m'] = 2.1
        with self.assertRaisesRegex(RuntimeError, 'inventory changed'):
            nv._fault_encoding_exclusion_evidence(changed)

    @unittest.skipIf(nv.shapely_shape is None, 'Shapely not installed')
    def test_authoritative_nevada_clip_intersects_cross_border_polygon(self):
        clip = nv._load_nv_clip()
        crossing = {
            'type': 'Polygon',
            'coordinates': [[[-114.2, 36.9], [-113.8, 36.9],
                             [-113.8, 37.1], [-114.2, 37.1],
                             [-114.2, 36.9]]],
        }
        geometry, flags = nv._clip_polygon(crossing, clip)
        self.assertTrue(flags['changed'])
        self.assertFalse(flags['outside'])
        self.assertIsNone(flags['repair'])
        self.assertTrue(clip['boundary'].buffer(1e-9).covers(
            nv.shapely_shape(geometry)))
        self.assertRegex(clip['manifest']['artifact_sha256'], r'^[0-9a-f]{64}$')

    @unittest.skipIf(nv.shapely_shape is None, 'Shapely not installed')
    def test_invalid_polygon_repair_is_returned_as_evidence(self):
        clip = nv._load_nv_clip()
        invalid = {
            'type': 'Polygon',
            'coordinates': [[[-118.2, 39.0], [-118.0, 39.2],
                             [-118.2, 39.2], [-118.0, 39.0],
                             [-118.2, 39.0]]],
        }
        geometry, flags = nv._clip_polygon(invalid, clip)
        repair = flags['repair']
        self.assertIsNotNone(repair)
        self.assertIn('Self-intersection', repair['validity_reason'])
        self.assertEqual(repair['nonpolygon_parts_dropped_by_type'], {})
        self.assertIn(geometry['type'], ('Polygon', 'MultiPolygon'))

    def test_ds249_extractor_requires_complete_member_inventory(self):
        with tempfile.TemporaryDirectory() as directory:
            valid = os.path.join(directory, 'valid.zip')
            with zipfile.ZipFile(valid, 'w') as archive:
                for members in nv.DS249_MEMBERS.values():
                    for name in members:
                        archive.writestr(name, b'x' * 110_000)
            extracted = os.path.join(directory, 'extracted')
            os.mkdir(extracted)
            paths = nv._extract_ds249(valid, extracted)
            self.assertTrue(os.path.isfile(paths['geology']))
            self.assertTrue(os.path.isfile(paths['faults']))

            invalid = os.path.join(directory, 'invalid.zip')
            with zipfile.ZipFile(invalid, 'w') as archive:
                archive.writestr(nv.DS249_MEMBERS['geology'][0], b'x')
            with self.assertRaisesRegex(RuntimeError, 'lacks required members'):
                nv._extract_ds249(invalid, directory)

    def test_manifest_entries_preserve_explicit_ds249_nulls_and_count_drift(self):
        stats = {
            'geology': {
                'source_records': 3, 'n': 1, 'null_geometry_count': 2,
                'null_geometry_source_record_ids': ['28814', '30918'],
                'geometry_inventory': nv.DS249_GEOMETRY_CONTRACT['geology'],
                'topology_repair': {'count': 187}},
            'faults': {
                'source_records': 2, 'n': 2, 'null_geometry_count': 0,
                'null_geometry_source_record_ids': [],
                'geometry_inventory': nv.DS249_GEOMETRY_CONTRACT['faults'],
                'topology_repair': {'count': 0}},
        }
        artifact = {
            'bytes': 200, 'sha256': 'a' * 64,
            'bounds': [-120.0, 35.0, -114.1, 42.0],
            'semantic_tile_feature_counts': {'nv_ds249_geology': 3},
        }
        entry = nv._ds249_entry(
            stats, {'bytes': nv.DS249_BYTES, 'sha256': nv.DS249_SHA256}, artifact)
        self.assertEqual(entry['n'], 3)
        self.assertEqual(entry['by_layer']['geology']['null_geometry_count'], 2)
        district = nv._district_entry(
            {'oid_field': 'OBJECTID_1', 'ids': list(range(1, 536)),
             'id_sha256': 'b' * 64, 'metadata_sha256': 'c' * 64},
            {'source_records': 535, 'n': 535, 'empty_geometry_count': 0,
             'empty_geometry_object_ids': [], 'topology_repair': None,
             'spatial_clip': {
                 'fully_outside_count': 0, 'fully_outside_object_ids': [],
                 'fully_outside_object_ids_sha256': 'e' * 64,
                 'geometry_clipped_count': 0,
                 'geometry_clipped_object_ids': [],
                 'geometry_clipped_object_ids_sha256': 'f' * 64,
                 'geometry_unchanged_count': 535,
                 'ordering': 'topology_repair_before_state_intersection',
                 'preclip_area_square_degrees': 1.0,
                 'postclip_area_square_degrees': 1.0,
                 'area_removed_square_degrees': 0.0}},
            {'authority': 'TIGERweb test', 'method': 'geometric intersection',
             'artifact': 'infra/state_clips.json',
             'artifact_sha256': 'd' * 64}, artifact)
        self.assertEqual(district['n'], 535)
        self.assertEqual(
            district['catalog_count_reconciliation']['status'],
            'documented_live_service_catalog_drift')
        self.assertNotEqual(
            district['catalog_count_reconciliation']['live_service_polygons'],
            district['catalog_count_reconciliation']['catalog_mapped_polygon_claim'])

    def test_multi_archive_publication_preserves_unrelated_manifest_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            site = os.path.join(directory, 'site')
            manifest = os.path.join(site, 'data', 'manifest.json')
            out_dir = os.path.join(site, 'data', 'tiles', 'states', 'nv')
            os.makedirs(out_dir)
            with open(manifest, 'w', encoding='utf-8') as output:
                json.dump({'national_baselines': {'keep': {'n': 7}}}, output)
            keys = {
                key: os.path.join(out_dir, key + '.pmtiles')
                for key in nv.BASELINE_KEYS
            }
            pending = {}
            for key in keys:
                path = os.path.join(directory, 'pending-' + key)
                with open(path, 'wb') as output:
                    output.write(key.encode())
                pending[key] = path
            entries = {key: {'status': 'baseline_not_release'} for key in keys}
            with mock.patch.object(nv, 'MANIFEST', manifest), \
                    mock.patch.object(nv, 'BASELINE_KEYS', keys):
                nv._publish(pending, entries)
            with open(manifest, encoding='utf-8') as source:
                stamped = json.load(source)['national_baselines']
            self.assertEqual(stamped['keep'], {'n': 7})
            self.assertEqual(set(stamped), {'keep', *keys})
            self.assertTrue(all(os.path.isfile(path) for path in keys.values()))

    def test_manifest_race_rolls_back_new_archives(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = os.path.join(directory, 'manifest.json')
            with open(manifest, 'w', encoding='utf-8') as output:
                json.dump({'national_baselines': {}}, output)
            keys = {key: os.path.join(directory, key + '.pmtiles')
                    for key in nv.BASELINE_KEYS}
            pending = {}
            for key in keys:
                path = os.path.join(directory, 'pending-' + key)
                with open(path, 'wb') as output:
                    output.write(b'new')
                pending[key] = path
            real_replace = os.replace
            changed = [False]

            def racing_replace(source, target):
                real_replace(source, target)
                if target == next(iter(keys.values())) and not changed[0]:
                    with open(manifest, 'ab') as output:
                        output.write(b' ')
                    changed[0] = True

            with mock.patch.object(nv, 'MANIFEST', manifest), \
                    mock.patch.object(nv, 'BASELINE_KEYS', keys), \
                    mock.patch.object(nv.os, 'replace', side_effect=racing_replace):
                with self.assertRaisesRegex(RuntimeError, 'manifest changed'):
                    nv._publish(pending, {key: {} for key in keys})
            self.assertFalse(any(os.path.exists(path) for path in keys.values()))

    @unittest.skipUnless(shutil.which('tippecanoe'), 'tippecanoe not installed')
    def test_real_pmtiles_full_scan_retains_provenance(self):
        with tempfile.TemporaryDirectory() as directory:
            sequence = os.path.join(directory, 'feature.geojsonseq')
            archive = os.path.join(directory, 'feature.pmtiles')
            props = {
                'fid': 1, 'st': 'NV', 'source_dataset': 'fixture',
                'source_id': 'fixture:1', 'source_scale': '1:250,000',
                'source_scale_status': 'fixture', 'source_ref': 'Test map',
                'source_url': 'https://example.test/map',
                'publication_id': 'Fixture 1',
            }
            with open(sequence, 'w', encoding='utf-8') as output:
                output.write(json.dumps({
                    'type': 'Feature', 'id': 1, 'properties': props,
                    'geometry': POLYGON}, separators=(',', ':')) + '\n')
            nv._run_tippecanoe(
                archive, (('nv_nbmg_onegeology_250k', sequence),),
                'Test fixture', 4)
            meta = nv._validate_pmtiles(
                archive, ('nv_nbmg_onegeology_250k',))
            self.assertGreater(
                meta['semantic_layer_counts']['nv_nbmg_onegeology_250k'], 0)
            self.assertEqual(meta['source_layers'], ['nv_nbmg_onegeology_250k'])


if __name__ == '__main__':
    unittest.main()
