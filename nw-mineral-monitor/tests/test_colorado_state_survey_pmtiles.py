"""Adversarial contracts for the private Colorado state-survey baseline."""
import importlib
import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
import zipfile
from unittest import mock


ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), '..'))
PIPELINES = os.path.join(ROOT, 'pipelines')
sys.path.insert(0, PIPELINES)

co = importlib.import_module('build_colorado_state_survey_pmtiles')


def arc_feature(oid=1, **properties):
    value = {'OBJECTID': oid}
    value.update(properties)
    return {
        'type': 'Feature', 'properties': value,
        'geometry': {'type': 'LineString',
                     'coordinates': [[-105.1, 39.1], [-105.0, 39.2]]},
    }


class ColoradoStateSurveyTests(unittest.TestCase):
    def test_default_is_private_double_build_and_three_pmtiles(self):
        self.assertEqual(set(co.BASELINE_KEYS), {
            'co_usgs_cngm_tweto_500k', 'co_cgs_on006_faults',
            'co_cgs_on007_districts'})
        self.assertTrue(all(path.endswith('.pmtiles')
                            for path in co.BASELINE_KEYS.values()))
        self.assertTrue(all('/tiles/states/co/' in path
                            for path in co.BASELINE_KEYS.values()))
        self.assertNotEqual(
            os.path.commonpath((os.path.realpath(co.SITE),
                                os.path.realpath(co.PRIVATE_STAGING_ROOT))),
            os.path.realpath(co.SITE))
        with mock.patch.object(co, 'build') as build:
            co.main([])
        build.assert_called_once_with(
            publish=False, grace_seconds=0, double_build=True)

    def test_source_counts_and_tweto_linkage_are_exact(self):
        self.assertEqual(co.CNGM_WHERE, "MapSourceID='map50'")
        self.assertEqual(co.CNGM_MAP_SOURCE['DataSources_ID'], 'map50')
        self.assertEqual(co.CNGM_DATA_SOURCE['DataSources_ID'], '1035')
        self.assertEqual(co.CNGM_DATA_SOURCE['URL'],
                         'https://ngmdb.usgs.gov/Prodesc/proddesc_68589.htm')
        self.assertEqual(co.ARCGIS_SNAPSHOT_CONTRACTS[
            'cngm_geology']['n'], 9_500)
        self.assertEqual(co.ARCGIS_SNAPSHOT_CONTRACTS[
            'cngm_faults']['n'], 10_238)
        self.assertEqual(co.ARCGIS_SNAPSHOT_CONTRACTS[
            'cgs_quaternary']['n'], 864)
        self.assertEqual(co.ARCGIS_SNAPSHOT_CONTRACTS[
            'cgs_cenozoic']['n'], 2_698)
        self.assertTrue(all(
            value is not None and len(value) == 64
            for value in co.SOURCE_SEQUENCE_SHA256.values()))

    def test_cngm_source_binding_checks_both_map_and_data_rows(self):
        responses = [
            {'features': [{'attributes': co.CNGM_MAP_SOURCE}]},
            {'features': [{'attributes': co.CNGM_DATA_SOURCE}]},
        ]
        with mock.patch.object(
                co, '_request_json', side_effect=responses) as request:
            result = co._verify_cngm_source_bindings()
        self.assertEqual(result['map_source'], co.CNGM_MAP_SOURCE)
        self.assertEqual(result['data_source'], co.CNGM_DATA_SOURCE)
        self.assertEqual(request.call_count, 2)

        changed = dict(co.CNGM_DATA_SOURCE, Source='changed in place')
        with mock.patch.object(co, '_request_json', side_effect=[
                responses[0], {'features': [{'attributes': changed}]}]):
            with self.assertRaisesRegex(RuntimeError, 'data_source'):
                co._verify_cngm_source_bindings()

    def test_arcgis_snapshot_requires_typed_oid_and_exact_ids(self):
        key = 'fixture'
        metadata = {
            'name': 'Fixture', 'geometryType': 'esriGeometryPolyline',
            'description': '', 'copyrightText': '', 'maxRecordCount': 100,
            'fields': [
                {'name': 'OBJECTID', 'alias': 'OBJECTID',
                 'type': 'esriFieldTypeOID', 'length': None},
            ],
        }
        selected = co._selected_layer_metadata(metadata)
        ids = [1, 2]
        spec = {
            'url': 'https://example.test/0', 'where': '1=1',
            'name': 'Fixture', 'geometry_type': 'esriGeometryPolyline',
            'fields': ('OBJECTID',), 'kind': 'line', 'layer': 'fixture',
        }
        contract = {
            'object_id_field': 'OBJECTID', 'n': 2,
            'minimum_object_id': 1, 'maximum_object_id': 2,
            'object_ids_sha256': co._canonical_sha256(ids),
            'layer_metadata_sha256': co._canonical_sha256(selected),
        }
        with mock.patch.dict(co.SOURCE_SPECS, {key: spec}, clear=True), \
                mock.patch.dict(
                    co.ARCGIS_SNAPSHOT_CONTRACTS, {key: contract}, clear=True), \
                mock.patch.object(co, '_request_json', side_effect=[
                    metadata,
                    {'objectIdFieldName': 'OBJECTID', 'objectIds': [2, 1]},
                ]):
            self.assertEqual(co._layer_snapshot(key)['ids'], ids)

        metadata['fields'][0]['type'] = 'esriFieldTypeInteger'
        with mock.patch.dict(co.SOURCE_SPECS, {key: spec}, clear=True), \
                mock.patch.dict(
                    co.ARCGIS_SNAPSHOT_CONTRACTS, {key: contract}, clear=True), \
                mock.patch.object(co, '_request_json', side_effect=[
                    metadata,
                    {'objectIdFieldName': 'OBJECTID', 'objectIds': ids},
                ]):
            with self.assertRaisesRegex(RuntimeError, 'typed object-ID'):
                co._layer_snapshot(key)

    def test_arcgis_pages_are_exact_post_requests(self):
        key = 'fixture'
        spec = {
            'url': 'https://example.test/0', 'where': '1=1',
            'fields': ('OBJECTID',), 'kind': 'line', 'layer': 'fixture',
        }
        pages = [
            {'features': [arc_feature(1), arc_feature(2)]},
            {'features': [arc_feature(3)]},
        ]
        snapshot = {'oid_field': 'OBJECTID', 'ids': [1, 2, 3]}
        with mock.patch.dict(co.SOURCE_SPECS, {key: spec}, clear=True), \
                mock.patch.object(
                    co, '_request_json', side_effect=pages) as request:
            self.assertEqual(len(list(co._iter_snapshot(
                key, snapshot, page=2))), 3)
        self.assertEqual(request.call_args_list[0].args[1]['objectIds'], '1,2')
        self.assertTrue(request.call_args_list[0].kwargs['post'])

        with mock.patch.dict(co.SOURCE_SPECS, {key: spec}, clear=True), \
                mock.patch.object(co, '_request_json', return_value={
                    'features': [arc_feature(2), arc_feature(1)]}):
            with self.assertRaisesRegex(RuntimeError, 'does not match pinned IDs'):
                list(co._iter_snapshot(
                    key, {'oid_field': 'OBJECTID', 'ids': [1, 2]}, page=2))

    def test_normalization_preserves_literal_cngm_linkage(self):
        raw = {'properties': {
            'OBJECTID': 7, 'MapSourceID': 'map50', 'DataSourceID': '1035',
            'MapUnit': 'Qa', 'IdentityConfidence': 'certain', 'Label': 'Qa',
            'Symbol': '1', 'Notes': None, 'MapUnitPolys_ID': 'mapunit-7',
            'Source_MapUnit': 'Qa',
        }}
        with mock.patch.object(
                co, 'shapely_mapping', return_value={'type': 'Polygon'}):
            result = co._normalize_arcgis('cngm_geology', raw, 7, object())
        props = result['properties']
        self.assertEqual(props['MapSourceID'], 'map50')
        self.assertEqual(props['DataSourceID'], '1035')
        self.assertEqual(props['map_source_id'], 'map50')
        self.assertEqual(props['data_source_id'], '1035')
        self.assertEqual(props['source_map_citation'],
                         co.CNGM_MAP_SOURCE['Source'])
        self.assertEqual(props['data_source_citation'],
                         co.CNGM_DATA_SOURCE['Source'])

        raw['properties']['DataSourceID'] = 'changed'
        with self.assertRaisesRegex(RuntimeError, 'DataSourceID'):
            co._normalize_arcgis('cngm_geology', raw, 7, object())

    def test_on006_semantics_remain_exact_and_separate(self):
        raw = {'properties': {'OBJECTID_1': 9, 'OBJECTID': 4,
                              'NAME': 'Example fault'}}
        with mock.patch.object(
                co, 'shapely_mapping', return_value={'type': 'LineString'}):
            quaternary = co._normalize_arcgis(
                'cgs_quaternary', raw, 9, object())['properties']
            cenozoic = co._normalize_arcgis(
                'cgs_cenozoic', raw, 9, object())['properties']
        self.assertEqual(quaternary['fault_age_scope'], 'Quaternary')
        self.assertEqual(cenozoic['fault_age_scope'], 'Cenozoic')
        self.assertNotIn('active', json.dumps(
            co.BROWSER_LAYER_CONTRACTS).lower())

    def test_district_zip_and_typed_shapefile_contracts_are_complete(self):
        self.assertEqual(co.DISTRICT_BYTES, 202_712_855)
        self.assertEqual(len(co.DISTRICT_ARCHIVE_INVENTORY), 14)
        self.assertEqual(
            co._canonical_sha256(co.DISTRICT_ARCHIVE_INVENTORY),
            co.DISTRICT_ARCHIVE_INVENTORY_SHA256)
        self.assertEqual(co.DISTRICT_SCHEMA['geometry'], 'Polygon')
        self.assertEqual(set(co.DISTRICT_SCHEMA['properties']), {
            'Source', 'District', 'WebPage', 'County_1', 'County_2', 'Note'})
        with tempfile.TemporaryDirectory() as directory:
            archive = os.path.join(directory, 'changed.zip')
            with zipfile.ZipFile(archive, 'w') as output:
                output.writestr('unreviewed.txt', 'x')
            with self.assertRaisesRegex(RuntimeError, 'inventory changed'):
                co._extract_district_shapefile(archive, directory)

    def test_geometry_evidence_pins_empty_outside_clip_and_repair(self):
        self.assertEqual(co.GEOMETRY_CONTRACTS[
            'cngm_geology']['repair_count'], 17)
        self.assertEqual(co.GEOMETRY_CONTRACTS[
            'cngm_geology']['clipped_count'], 158)
        self.assertEqual(co.GEOMETRY_CONTRACTS[
            'districts']['repair_count'], 1)
        self.assertEqual(co.GEOMETRY_CONTRACTS[
            'districts']['clipped_count'], 1)
        for contract in co.GEOMETRY_CONTRACTS.values():
            self.assertEqual(contract['empty_count'], 0)
            self.assertEqual(contract['fully_outside_count'], 0)
            self.assertEqual(contract['empty_sha256'], co.EMPTY_SHA256)
            self.assertEqual(contract['fully_outside_sha256'], co.EMPTY_SHA256)

    def test_tippecanoe_is_lossless_and_generator_options_are_path_free(self):
        completed = types.SimpleNamespace(returncode=0)
        with tempfile.TemporaryDirectory() as directory:
            output = os.path.join(directory, 'usgs-cngm-tweto-500k.pmtiles')
            geology = os.path.join(directory, 'cngm_geology.geojsonseq')
            faults = os.path.join(directory, 'cngm_faults.geojsonseq')
            with mock.patch.object(
                    co.subprocess, 'run', return_value=completed) as run:
                co._run_tippecanoe(output, (
                    ('co_cngm_tweto_geology', geology),
                    ('co_cngm_tweto_faults', faults)),
                    co.ARCHIVE_ATTRIBUTIONS[os.path.basename(output)])
        command = run.call_args.args[0]
        self.assertEqual(run.call_args.kwargs['cwd'], os.path.realpath(directory))
        self.assertIn('--no-feature-limit', command)
        self.assertIn('--no-tile-size-limit', command)
        self.assertIn('--no-tiny-polygon-reduction-at-maximum-zoom', command)
        self.assertNotIn('--read-parallel', command)
        self.assertNotIn('--drop-densest-as-needed', command)
        self.assertTrue(all('/' not in token and '\\' not in token
                            for token in command[1:]))

    def test_private_tile_sets_use_separate_relative_input_copies(self):
        with tempfile.TemporaryDirectory() as directory:
            source_dir = os.path.join(directory, 'source')
            one = os.path.join(directory, 'independent-a')
            two = os.path.join(directory, 'unrelated-b')
            os.makedirs(source_dir)
            sequences = {}
            for key in (*co.SOURCE_SPECS, 'districts'):
                path = os.path.join(source_dir, key + '.seq')
                with open(path, 'wb') as output:
                    output.write(key.encode())
                sequences[key] = path

            calls = []

            def record(output, layers, attribution):
                calls.append((output, layers, attribution))
                with open(output, 'wb') as target:
                    target.write(b'fixture')

            with mock.patch.object(co, '_run_tippecanoe', side_effect=record):
                co._tile_set(one, sequences)
                co._tile_set(two, sequences)
            self.assertEqual(len(calls), 6)
            for output, layers, _ in calls:
                self.assertTrue(all(os.path.dirname(sequence) ==
                                    os.path.dirname(output)
                                    for _, sequence in layers))
            self.assertEqual(
                [[os.path.basename(sequence) for _, sequence in row[1]]
                 for row in calls[:3]],
                [[os.path.basename(sequence) for _, sequence in row[1]]
                 for row in calls[3:]])

    def test_pmtiles_metadata_guard_rejects_any_private_path(self):
        path = os.path.join('/private/build', 'cgs-on006-faults.pmtiles')
        stable = {
            'name': 'co_cgs_on006_faults',
            'description': 'co_cgs_on006_faults',
            'attribution': co.ARCHIVE_ATTRIBUTIONS[os.path.basename(path)],
            'generator': f'tippecanoe {co.TIPPECANOE_VERSION}',
            'generator_options': ('tippecanoe --output '
                                  'cgs-on006-faults.pmtiles'),
            'vector_layers': [],
        }
        with mock.patch.object(co, '_pmtiles_json_metadata', return_value=stable):
            result = co._assert_path_independent_metadata(
                path, 'co_cgs_on006_faults')
        self.assertEqual(
            result['status'], 'complete_path_free_reproducible_metadata')

        changed = dict(stable, generator_options='tippecanoe /private/in.seq')
        with mock.patch.object(co, '_pmtiles_json_metadata', return_value=changed):
            with self.assertRaisesRegex(RuntimeError, 'path-free'):
                co._assert_path_independent_metadata(
                    path, 'co_cgs_on006_faults')

    def test_full_maxzoom_id_inventory_rejects_one_missing_record(self):
        snapshot = {'ids': [1, 2, 3]}
        stats = {
            'source_records': 3, 'empty_geometry_object_ids': [],
            'spatial_clip': {'fully_outside_object_ids': []},
        }
        metadata = {
            'maxzoom_feature_ids': {'layer': [1, 2, 3]},
            'maxzoom_feature_instances': {'layer': 4},
        }
        result = co._assert_unique_ids(
            'fixture', 'layer', snapshot, stats, metadata)
        self.assertEqual(result['unique_maxzoom_ids'], 3)
        metadata['maxzoom_feature_ids']['layer'] = [1, 2]
        with self.assertRaisesRegex(RuntimeError, 'do not reconcile'):
            co._assert_unique_ids(
                'fixture', 'layer', snapshot, stats, metadata)

    def test_browser_descriptors_are_complete_and_lazy(self):
        stats = {
            key: {'n': contract['n']}
            for key, contract in co.ARCGIS_SNAPSHOT_CONTRACTS.items()}
        stats['districts'] = {'n': 383}
        metadata = {'bounds': [-109.0, 37.0, -102.1, 41.0],
                    'minzoom': 0, 'maxzoom': 12}
        descriptor = co._browser_descriptor(
            'co_usgs_cngm_tweto_500k',
            'data/tiles/states/co/usgs-cngm-tweto-500k.pmtiles',
            metadata, stats)
        self.assertTrue(descriptor['lazy'])
        self.assertEqual(descriptor['activation_zoom'], 4)
        self.assertEqual(len(descriptor['layers']), 2)
        self.assertEqual([row['feature_count'] for row in descriptor['layers']],
                         [9_500, 10_238])
        for row in descriptor['layers']:
            self.assertFalse(row['default_visible'])
            self.assertTrue(row['required_properties'])
            self.assertEqual(row['bounds'], metadata['bounds'])
            self.assertIn(row['geometry'], {'polygon', 'line'})
            self.assertIn(row['style']['type'], {'fill', 'line'})
        manifest = {
            'sentinel': {'nested': [1, 2, 3]},
            'national_baselines': {
                'unrelated': {'sha256': 'a' * 64},
                'co_usgs_cngm_tweto_500k': {'old': True},
            },
        }
        before = co._unrelated_manifest_sha256(manifest)
        manifest['national_baselines'].update({
            key: {'replacement': key} for key in co.BASELINE_KEYS})
        self.assertEqual(co._unrelated_manifest_sha256(manifest), before)
        manifest['sentinel']['nested'].append(4)
        self.assertNotEqual(co._unrelated_manifest_sha256(manifest), before)

    def test_publication_refuses_unpinned_or_partial_atomic_set(self):
        with mock.patch.dict(co.SOURCE_SEQUENCE_SHA256, {
                **co.SOURCE_SEQUENCE_SHA256, 'districts': None}, clear=True):
            with self.assertRaisesRegex(RuntimeError, 'not pinned'):
                co._publish({}, {})
        with self.assertRaisesRegex(RuntimeError, 'exact atomic set'):
            co._publish({}, {})

    def test_atomic_publication_restores_archives_on_base_exception(self):
        with tempfile.TemporaryDirectory() as directory:
            site = os.path.join(directory, 'site')
            public = os.path.join(site, 'data', 'tiles', 'states', 'co')
            staging = os.path.join(directory, 'staging')
            os.makedirs(public)
            os.makedirs(staging)
            manifest = os.path.join(site, 'data', 'manifest.json')
            os.makedirs(os.path.dirname(manifest), exist_ok=True)
            with open(manifest, 'w', encoding='utf-8') as output:
                json.dump({'national_baselines': {}, 'sentinel': 1}, output)
            keys = {
                key: os.path.join(public, f'{index}.pmtiles')
                for index, key in enumerate(co.BASELINE_KEYS)}
            pending = {}
            for key, final in keys.items():
                with open(final, 'wb') as output:
                    output.write(('old-' + key).encode())
                pending[key] = os.path.join(staging, key + '.pmtiles')
                with open(pending[key], 'wb') as output:
                    output.write(('new-' + key).encode())
            entries = {
                key: {'status': 'baseline_not_release'} for key in keys}
            original_replace = os.replace

            def interrupt_manifest(source, target):
                if (target == manifest and os.path.basename(source).startswith(
                        '.manifest-co-state-survey-') and
                        'original-' not in os.path.basename(source)):
                    raise KeyboardInterrupt('fixture interruption')
                return original_replace(source, target)

            with mock.patch.object(co, 'MANIFEST', manifest), \
                    mock.patch.object(co, 'OUT_DIR', public), \
                    mock.patch.object(co, 'BASELINE_KEYS', keys), \
                    mock.patch.object(
                        co.os, 'replace', side_effect=interrupt_manifest):
                with self.assertRaises(KeyboardInterrupt):
                    co._publish(pending, entries)
            for key, final in keys.items():
                with open(final, 'rb') as source:
                    self.assertEqual(source.read(), ('old-' + key).encode())
            with open(manifest, encoding='utf-8') as source:
                self.assertEqual(json.load(source)['sentinel'], 1)


if __name__ == '__main__':
    unittest.main()
