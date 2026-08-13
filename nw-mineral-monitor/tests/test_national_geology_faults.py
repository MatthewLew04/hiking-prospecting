import importlib
import json
import os
import shutil
import sys
import tempfile
import unittest
import zipfile
from unittest import mock


ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), '..'))
PIPELINES = os.path.join(ROOT, 'pipelines')
sys.path.insert(0, PIPELINES)

geo = importlib.import_module('build_national_geology_faults_pmtiles')


def arc_feature(oid, *, properties=None, geometry=None):
    values = {'OBJECTID': oid}
    values.update(properties or {})
    feature = {'type': 'Feature', 'properties': values}
    if geometry is not None:
        feature['geometry'] = geometry
    return feature


POLYGON = {
    'type': 'Polygon',
    'coordinates': [[[-120.0, 40.0], [-119.9, 40.0],
                     [-119.9, 40.1], [-120.0, 40.0]]],
}
LINE = {
    'type': 'LineString',
    'coordinates': [[-120.0, 40.0], [-119.9, 40.1]],
}


class NationalGeologyFaultTests(unittest.TestCase):
    def test_scope_is_exactly_49_without_hawaii_or_territories(self):
        self.assertEqual(len(geo.TARGET_STATES), 49)
        self.assertEqual(len(geo.CONUS_STATES), 48)
        self.assertIn('AK', geo.TARGET_STATES)
        self.assertNotIn('AK', geo.CONUS_STATES)
        self.assertFalse(geo.TARGET_STATES & {'DC', 'HI', 'PR'})
        self.assertTrue(geo.SGMC_SERVICE.endswith('/FeatureServer'))
        self.assertTrue(geo.AK_SERVICE.endswith('/FeatureServer'))
        self.assertTrue(geo.QFAULTS_URL.endswith('/Qfaults_GIS.zip'))

    def test_source_scale_parser_preserves_explicit_ranges_and_unknown(self):
        self.assertEqual(
            geo._source_scale('Map, scale ca. 1:250,000.'),
            ('1:250,000', 'explicit'))
        self.assertEqual(
            geo._source_scale('Compiled at 1:50,000 to 1:100,000.'),
            ('1:50,000 to 1:100,000', 'explicit'))
        self.assertEqual(
            geo._source_scale('Publication without a stated denominator.'),
            ('not stated in source reference',
             'source_reference_omits_scale'))
        with self.assertRaisesRegex(RuntimeError, 'reference is empty'):
            geo._source_scale(None)

    def test_object_id_snapshot_pages_are_exact_and_posted(self):
        pages = [
            {'features': [arc_feature(2), arc_feature(7)]},
            {'features': [arc_feature(11)]},
        ]
        with mock.patch.object(geo, '_snapshot_ids', return_value=(
                'OBJECTID', [2, 7, 11])), \
                mock.patch.object(geo, '_request_json', side_effect=pages) as request:
            got = list(geo._iter_snapshot(
                'https://example.test/FeatureServer/0', '1=1',
                ('OBJECTID', 'SOURCE'), geometry=True, page=2))
        self.assertEqual([row['properties']['OBJECTID'] for row in got], [2, 7, 11])
        self.assertEqual(request.call_count, 2)
        self.assertTrue(all(call.kwargs['post'] for call in request.call_args_list))
        parameters = [call.args[1] for call in request.call_args_list]
        self.assertEqual([item['objectIds'] for item in parameters], ['2,7', '11'])
        self.assertEqual([item['orderByFields'] for item in parameters],
                         ['OBJECTID ASC', 'OBJECTID ASC'])
        self.assertEqual([item['geometryPrecision'] for item in parameters],
                         [8, 8])
        self.assertTrue(all('maxAllowableOffset' not in item
                            for item in parameters))

    def test_object_id_snapshot_page_mismatch_fails_loudly(self):
        with mock.patch.object(geo, '_snapshot_ids', return_value=(
                'OBJECTID', [2, 7])), \
                mock.patch.object(geo, '_request_json', return_value={
                    'features': [arc_feature(2), arc_feature(8)]}):
            with self.assertRaisesRegex(RuntimeError, 'snapshot page mismatch'):
                list(geo._iter_snapshot(
                    'https://example.test/FeatureServer/0', '1=1',
                    ('OBJECTID',), geometry=False))

    def test_sgmc_polygon_has_per_feature_source_and_scale(self):
        raw = arc_feature(9, properties={
            'STATE': 'NV', 'SGMC_LABEL': 'Tv', 'UNIT_NAME': 'Volcanic rocks',
            'GENERALIZED_LITH': 'Igneous, volcanic',
            'REFERENCE': 'Example Survey, 1999, map, scale 1:250,000.',
            'DIGITAL_URL': 'https://example.test/source',
        }, geometry=POLYGON)
        state, status, feature = geo._normalize_sgmc_geology(raw, 41)
        props = feature['properties']
        self.assertEqual((state, status), ('NV', 'explicit'))
        self.assertEqual(feature['id'], 41)
        self.assertEqual(props['source_dataset'], 'usgs_sgmc_v1_1')
        self.assertTrue(props['source_id'].startswith('sgmc:NV:'))
        self.assertEqual(props['source_scale'], '1:250,000')
        self.assertIn('scale 1:250,000', props['source_ref'])
        with self.assertRaisesRegex(RuntimeError, 'unsupported state'):
            leaked = dict(raw)
            leaked['properties'] = dict(raw['properties'], STATE='HI')
            geo._normalize_sgmc_geology(leaked, 42)

    def test_alaska_source_join_is_mandatory(self):
        raw = arc_feature(1, properties={
            'SOURCE': 'AC002', 'STATE_LABEL': 'KJ',
            'STATE_UNITNAME': 'Example unit', 'AGE_RANGE': 'Mesozoic',
        }, geometry=POLYGON)
        refs = {'AC002': 'Example Alaska map, scale 1:200,000.'}
        state, status, feature = geo._normalize_ak_geology(raw, 1, refs)
        self.assertEqual((state, status), ('AK', 'explicit'))
        self.assertEqual(feature['properties']['source_id'], 'sim3340:AC002')
        self.assertEqual(feature['properties']['source_scale'], '1:200,000')
        reserved = json.loads(json.dumps(raw))
        reserved['properties']['SOURCE'] = 'AC001'
        _, reserved_status, reserved_feature = geo._normalize_ak_geology(
            reserved, 2, {})
        self.assertEqual(reserved_status, 'reserved_001_no_reference_row')
        self.assertEqual(reserved_feature['properties']['source_scale'],
                         'nominal 1:250,000 compilation')
        self.assertIn('nsarefs table contains no citation row',
                      reserved_feature['properties']['source_ref'])
        with self.assertRaisesRegex(RuntimeError, 'no SIM 3340 reference'):
            missing = json.loads(json.dumps(raw))
            missing['properties']['SOURCE'] = 'AC999'
            geo._normalize_ak_geology(missing, 3, {})

    def test_qfault_hawaii_is_excluded_and_unspecified_is_not_blank(self):
        raw = {
            'properties': {
                '_source_record_id': '5',
                'Location': 'Nevada', 'fault_id': '123',
                'fault_name': 'Example fault', 'scale': 'unspecified',
                'linetype': 'Well Constrained', 'cooperator': 'NBMG',
            },
            'geometry': LINE,
        }
        state, status, feature = geo._normalize_qfault(raw, 5)
        self.assertEqual((state, status), ('NV', 'source_marks_unspecified'))
        self.assertEqual(feature['properties']['source_scale'], 'unspecified')
        self.assertEqual(feature['properties']['source_id'], 'qfaults:123')
        hawaii = json.loads(json.dumps(raw))
        hawaii['properties']['Location'] = 'Hawaii'
        self.assertIsNone(geo._normalize_qfault(hawaii, 6))
        unknown = json.loads(json.dumps(raw))
        unknown['properties']['Location'] = 'Atlantis'
        with self.assertRaisesRegex(RuntimeError, 'unmapped location'):
            geo._normalize_qfault(unknown, 7)

    def test_qfault_offshore_missing_catalog_id_uses_labeled_source_row(self):
        raw = {
            'properties': {
                '_source_record_id': '614', 'Location': 'CA offshore',
                'FAULT_ID': None, 'FAULT_NAME': 'unspecified',
                'MAPPED_SCA': '1:35,000', 'FLT_AGE': 'Quaternary',
                'LINE_TYPE': 'Accurately Located',
                'FLT_SOURCE': 'Golden, 2013; Johnson and others, 2019.',
            },
            'geometry': LINE,
        }
        state, status, feature = geo._normalize_qfault(raw, 91, offshore=True)
        self.assertEqual((state, status), ('CA', 'explicit'))
        self.assertEqual(feature['properties']['source_id'],
                         'qfaults:ca_offshore:row:614')
        self.assertEqual(feature['properties']['source_catalog_id_status'],
                         'absent_in_source')
        missing_row = json.loads(json.dumps(raw))
        del missing_row['properties']['_source_record_id']
        with self.assertRaisesRegex(RuntimeError, 'lacks fault ID'):
            geo._normalize_qfault(missing_row, 92, offshore=True)

    def test_subcentimetre_line_normalization_is_minimal_and_audited(self):
        raw = {
            'properties': {
                '_source_record_id': '46', 'Location': 'New Mexico',
                'fault_id': '2104', 'fault_name': 'Cuchillo Negro fault zone',
                'scale': '1:24,000', 'linetype': 'Well Constrained',
                'cooperator': 'New Mexico Bureau of Geology',
            },
            'geometry': {
                'type': 'LineString',
                'coordinates': [
                    [-107.37328619, 33.27338865],
                    [-107.37328622, 33.27338869],
                ],
            },
        }
        source_hash = geo._geometry_sha256(raw['geometry'])
        state, _, feature = geo._normalize_qfault(raw, 46)
        props = feature['properties']
        feature['geometry'], audit = geo._normalize_collapsed_line_parts(
            feature['geometry'], props, props['source_record_id'])
        self.assertIsNotNone(audit)
        self.assertEqual(state, 'NM')
        self.assertEqual(props['source_geometry_sha256'], source_hash)
        self.assertEqual(props['source_record_id'], '46')
        self.assertEqual(props['geometry_normalization'],
                         geo.GEOMETRY_NORMALIZATION)
        self.assertEqual(props['geometry_normalization_engine'],
                         geo.GEOMETRY_NORMALIZATION_ENGINE)
        self.assertGreater(props['geometry_normalization_delta_m'], 0)
        self.assertLessEqual(props['geometry_normalization_delta_m'], 0.02)
        self.assertGreater(len({geo._quantized_world(position) for position in
                                feature['geometry']['coordinates']}), 1)
        # The source object was not mutated, and its cryptographic identity is
        # independent of the minimum tile-safe representation.
        self.assertEqual(raw['geometry']['coordinates'][1],
                         [-107.37328622, 33.27338869])

    def test_noncollapsed_line_is_not_normalized(self):
        props = {}
        geometry = json.loads(json.dumps(LINE))
        normalized, audit = geo._normalize_collapsed_line_parts(
            geometry, props, '9')
        self.assertIsNone(audit)
        self.assertEqual(normalized, LINE)
        self.assertNotIn('geometry_normalization', props)

    def test_subcentimetre_line_crossing_quantized_boundary_is_untouched(self):
        # This 2.5 mm source trace is smaller than one world-coordinate unit,
        # but its endpoints encode to different integers. It was retained by
        # the d20 diagnostic and must not be broadened into the repair set.
        geometry = {
            'type': 'LineString',
            'coordinates': [
                [-105.31779587, 35.89004630],
                [-105.31779589, 35.89004629],
            ],
        }
        self.assertEqual(len({geo._quantized_world(position)
                              for position in geometry['coordinates']}), 2)
        props = {}
        normalized, audit = geo._normalize_collapsed_line_parts(
            json.loads(json.dumps(geometry)), props, '44')
        self.assertIsNone(audit)
        self.assertEqual(normalized, geometry)

    @unittest.skipUnless(shutil.which('tippecanoe'), 'tippecanoe not installed')
    def test_lossless_build_repairs_only_diagnostic_missing_fid_then_matches_twice(self):
        common = {
            'st': 'NM', 'state': 'NM', 'src': 'usgs_qfaults_2020',
            'source_dataset': 'usgs_qfaults_2020',
            'source_scale': '1:24,000', 'source_scale_status': 'explicit',
            'source_ref': 'Reviewed test source, scale 1:24,000',
            'source_url': 'https://example.test/qfaults',
            'source_catalog_id_status': 'present',
        }
        features = [
            {
                'type': 'Feature', 'id': 1,
                'properties': dict(common, fid=1, source_id='qfaults:1',
                                   source_record_id='1'),
                'geometry': {'type': 'LineString', 'coordinates': [
                    [-107.5, 33.2], [-107.4, 33.3]]},
            },
            {
                'type': 'Feature', 'id': 2,
                'properties': dict(common, fid=2, source_id='qfaults:2104',
                                   source_record_id='55663'),
                'geometry': {'type': 'LineString', 'coordinates': [
                    [-107.37328619, 33.27338865],
                    [-107.37328622, 33.27338869]]},
            },
        ]
        with tempfile.TemporaryDirectory() as directory:
            sequence = os.path.join(directory, 'faults.geojsonseq')
            archive = os.path.join(directory, 'faults.pmtiles')
            with open(sequence, 'w', encoding='utf-8') as output:
                for feature in features:
                    geo._write_feature(output, feature)
            stats = {'geometry_normalizations': []}
            result = geo._build_twice(
                sequence, archive, 'faults', 'Reviewed test', 2, stats)
            with open(sequence, encoding='utf-8') as source:
                final_rows = [json.loads(line) for line in source]
        self.assertEqual(result['reconciliation']['diagnostic_missing_fids'], [2])
        self.assertEqual(result['reconciliation']['deterministic_builds'], 2)
        self.assertEqual([item['fid'] for item in stats['geometry_normalizations']], [2])
        self.assertNotIn('geometry_normalization', final_rows[0]['properties'])
        self.assertEqual(final_rows[1]['properties']['geometry_normalization'],
                         geo.GEOMETRY_NORMALIZATION)
        self.assertLessEqual(
            final_rows[1]['properties']['geometry_normalization_delta_m'], 0.02)

    def test_qfault_zip_requires_the_complete_shapefile_sets(self):
        required = (
            'SHP/Qfaults_US_Database.shp', 'SHP/Qfaults_US_Database.shx',
            'SHP/Qfaults_US_Database.dbf', 'SHP/Qfaults_US_Database.prj',
            'SHP/ca_offshore.shp', 'SHP/ca_offshore.shx',
            'SHP/ca_offshore.dbf', 'SHP/ca_offshore.prj')
        with tempfile.TemporaryDirectory() as directory:
            valid = os.path.join(directory, 'valid.zip')
            with zipfile.ZipFile(valid, 'w') as archive:
                for name in required:
                    archive.writestr(name, b'x')
            primary, offshore = geo._extract_qfaults(valid, directory)
            self.assertTrue(os.path.isfile(primary))
            self.assertTrue(os.path.isfile(offshore))
            invalid = os.path.join(directory, 'invalid.zip')
            with zipfile.ZipFile(invalid, 'w') as archive:
                archive.writestr(required[0], b'x')
            with self.assertRaisesRegex(RuntimeError, 'missing required members'):
                geo._extract_qfaults(invalid, directory)

    def test_build_stamps_both_archives_only_after_both_validate(self):
        counts = {state: 1 for state in sorted(geo.TARGET_STATES)}
        stats = {
            'n': 49, 'states': counts,
            'by_source': {'unit': 49},
            'source_scale_status': {'explicit': 49},
            'geometry_normalizations': [],
        }
        with tempfile.TemporaryDirectory() as directory:
            site = os.path.join(directory, 'site')
            manifest = os.path.join(site, 'data', 'manifest.json')
            geology_out = os.path.join(
                site, 'data', 'tiles', 'national', 'geology.pmtiles')
            faults_out = os.path.join(
                site, 'data', 'tiles', 'national', 'faults.pmtiles')
            os.makedirs(os.path.dirname(geology_out))
            with open(manifest, 'w', encoding='utf-8') as output:
                json.dump({'national_baselines': {'keep': {'n': 7}}}, output)
            with open(geology_out, 'wb') as output:
                output.write(b'old geology')
            with open(faults_out, 'wb') as output:
                output.write(b'old faults')

            def fake_tiles(sequence, output, layer, attribution):
                del sequence, attribution
                with open(output, 'wb') as archive:
                    archive.write(b'PMTiles\x03' + layer.encode())

            def fake_build(sequence, output, layer, attribution, source_records,
                           build_stats):
                del sequence, attribution, build_stats
                with open(output, 'wb') as archive:
                    archive.write(b'PMTiles\x03' + layer.encode())
                return {
                    'bytes': os.path.getsize(output), 'sha256': '1' * 64,
                    'reconciliation': {
                        'source_records': source_records,
                        'unique_tiled_fids': source_records,
                        'maxzoom': 12,
                        'maxzoom_unique_tiled_fids': source_records,
                        'missing_fid_count': 0, 'extra_fid_count': 0,
                        'deterministic_builds': 2,
                        'diagnostic_missing_fids': [],
                        'diagnostic_missing_fids_sha256': geo.hashlib.sha256(
                            b'[]').hexdigest(),
                    },
                }

            def fake_stream(path, *args):
                del args
                with open(path, 'w', encoding='utf-8') as output:
                    output.write('\n')
                return stats

            common = (
                mock.patch.object(geo, 'SITE', site),
                mock.patch.object(geo, 'MANIFEST', manifest),
                mock.patch.object(geo, 'GEOLOGY_OUT', geology_out),
                mock.patch.object(geo, 'FAULTS_OUT', faults_out),
                mock.patch.object(geo.shutil, 'which', return_value='/tippecanoe'),
                mock.patch.object(geo, 'fiona', mock.Mock()),
                mock.patch.object(geo, '_download', return_value={
                    'bytes': 1, 'sha256': '0' * 64}),
                mock.patch.object(geo, '_extract_qfaults', return_value=('a', 'b')),
                mock.patch.object(geo, '_ak_references', return_value={'A': 'scale 1:1'}),
                mock.patch.object(geo, '_stream_geology', side_effect=fake_stream),
                mock.patch.object(geo, '_stream_faults', side_effect=fake_stream),
                mock.patch.object(geo, '_run_tippecanoe', side_effect=fake_tiles),
                mock.patch.object(geo, '_build_twice', side_effect=fake_build),
            )
            for patcher in common:
                patcher.start()
            try:
                result = geo.build()
            finally:
                for patcher in reversed(common):
                    patcher.stop()
            with open(manifest, encoding='utf-8') as source:
                stamped = json.load(source)['national_baselines']
        self.assertEqual(stamped['keep'], {'n': 7})
        self.assertEqual(stamped['geology']['n'], 49)
        self.assertEqual(stamped['faults']['n'], 49)
        self.assertEqual(set(stamped['geology']['states']), geo.TARGET_STATES)
        self.assertEqual(result['geology']['features'], 49)

    def test_pair_publication_rolls_back_archives_and_manifest_on_baseexception(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = os.path.join(directory, 'manifest.json')
            geology_out = os.path.join(directory, 'geology.pmtiles')
            faults_out = os.path.join(directory, 'faults.pmtiles')
            pending_geology = os.path.join(directory, 'pending-geology.pmtiles')
            pending_faults = os.path.join(directory, 'pending-faults.pmtiles')
            files = {
                manifest: b'{"generation":"old"}',
                geology_out: b'old geology', faults_out: b'old faults',
                pending_geology: b'new geology', pending_faults: b'new faults',
            }
            for path, value in files.items():
                with open(path, 'wb') as output:
                    output.write(value)
            os.chmod(geology_out, 0o640)
            os.chmod(faults_out, 0o604)

            def interrupted_stamp(*args):
                del args
                with open(manifest, 'wb') as output:
                    output.write(b'{"generation":"new"}')
                raise KeyboardInterrupt('deterministic publish interruption')

            with mock.patch.object(geo, 'MANIFEST', manifest), \
                    mock.patch.object(geo, 'GEOLOGY_OUT', geology_out), \
                    mock.patch.object(geo, 'FAULTS_OUT', faults_out), \
                    mock.patch.object(geo, '_stamp_manifest',
                                      side_effect=interrupted_stamp):
                with self.assertRaisesRegex(KeyboardInterrupt, 'publish interruption'):
                    geo._publish_pair(
                        pending_geology, pending_faults, {'n': 1}, {'n': 1})
            with open(geology_out, 'rb') as source:
                self.assertEqual(source.read(), b'old geology')
            with open(faults_out, 'rb') as source:
                self.assertEqual(source.read(), b'old faults')
            with open(manifest, 'rb') as source:
                self.assertEqual(source.read(), b'{"generation":"old"}')
            self.assertEqual(os.stat(geology_out).st_mode & 0o777, 0o640)
            self.assertEqual(os.stat(faults_out).st_mode & 0o777, 0o604)
            self.assertFalse(any('rollback-' in name for name in os.listdir(directory)))

    def test_pair_publication_rolls_back_first_archive_if_second_replace_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = os.path.join(directory, 'manifest.json')
            geology_out = os.path.join(directory, 'geology.pmtiles')
            faults_out = os.path.join(directory, 'faults.pmtiles')
            pending_geology = os.path.join(directory, 'pending-geology.pmtiles')
            pending_faults = os.path.join(directory, 'pending-faults.pmtiles')
            for path, value in (
                    (manifest, b'{"generation":"old"}'),
                    (geology_out, b'old geology'), (faults_out, b'old faults'),
                    (pending_geology, b'new geology'),
                    (pending_faults, b'new faults')):
                with open(path, 'wb') as output:
                    output.write(value)
            real_replace = os.replace

            def ordered_replace(source, target):
                if source == pending_faults and target == faults_out:
                    raise OSError('deterministic second-archive failure')
                return real_replace(source, target)

            with mock.patch.object(geo, 'MANIFEST', manifest), \
                    mock.patch.object(geo, 'GEOLOGY_OUT', geology_out), \
                    mock.patch.object(geo, 'FAULTS_OUT', faults_out), \
                    mock.patch.object(geo.os, 'replace', side_effect=ordered_replace):
                with self.assertRaisesRegex(OSError, 'second-archive failure'):
                    geo._publish_pair(
                        pending_geology, pending_faults, {'n': 1}, {'n': 1})
            with open(geology_out, 'rb') as source:
                self.assertEqual(source.read(), b'old geology')
            with open(faults_out, 'rb') as source:
                self.assertEqual(source.read(), b'old faults')
            with open(manifest, 'rb') as source:
                self.assertEqual(source.read(), b'{"generation":"old"}')
            self.assertFalse(any('rollback-' in name for name in os.listdir(directory)))

    @unittest.skipUnless(shutil.which('tippecanoe'), 'tippecanoe not installed')
    def test_real_pmtiles_retains_fid_property(self):
        fixture = os.path.join(
            os.path.dirname(__file__), 'national_baseline_minimal.geojsonseq')
        with tempfile.TemporaryDirectory() as directory:
            archive = os.path.join(directory, 'geology.pmtiles')
            geo._run_tippecanoe(fixture, archive, 'geology', 'Test fixture')
            header = geo._validate_pmtiles(archive, 'geology', 1)
        self.assertEqual(header['version'], 3)
        self.assertGreater(header['semantic_layer_counts']['geology'], 0)
        geo._assert_lossless_inventory(header, 'geology', 1)

    def test_pmtiles_validation_requests_full_semantic_scan(self):
        parser = mock.Mock(return_value={'version': 3})
        fake_module = mock.Mock(_pmtiles_header=parser)
        with mock.patch.dict(sys.modules, {'validate_national': fake_module}):
            geo._validate_pmtiles('/tmp/test.pmtiles', 'faults', 19)
        parser.assert_called_once_with(
            '/tmp/test.pmtiles', ['faults'],
            {'faults': (
                'fid', 'st', 'state', 'src', 'source_dataset', 'source_id',
                'source_record_id', 'source_scale', 'source_scale_status',
                'source_ref', 'source_url')},
            verify_feature_properties=True, collect_feature_inventory=True,
            expected_source_records={'faults': 19})


if __name__ == '__main__':
    unittest.main()
