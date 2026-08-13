"""Adversarial contracts for the private Utah state-survey baseline."""
import copy
import importlib
import json
import os
import shutil
import stat
import sys
import tempfile
import types
import unittest
import zipfile
from unittest import mock


ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), '..'))
PIPELINES = os.path.join(ROOT, 'pipelines')
sys.path.insert(0, PIPELINES)

ut = importlib.import_module('build_utah_state_survey_pmtiles')


class UtahStateSurveyTests(unittest.TestCase):
    def test_default_is_private_double_build_and_exact_atomic_set(self):
        self.assertEqual(set(ut.BASELINE_KEYS), {
            'ut_ugs_map179dm_500k', 'ut_ugs_ds7_quaternary_faults',
            'ut_ugs_ofr695_mining_districts', 'ut_ugs_ofr757_umos'})
        self.assertEqual(ut.ATOMIC_GROUP_ID,
                         'ut_ugs_state_survey_baselines_v1')
        self.assertTrue(all(path.endswith('.pmtiles')
                            for path in ut.BASELINE_KEYS.values()))
        self.assertTrue(all('/tiles/states/ut/' in path
                            for path in ut.BASELINE_KEYS.values()))
        self.assertNotEqual(
            os.path.commonpath((os.path.realpath(ut.SITE),
                                os.path.realpath(ut.PRIVATE_STAGING_ROOT))),
            os.path.realpath(ut.SITE))
        with mock.patch.object(ut, 'build') as build:
            ut.main([])
        build.assert_called_once_with(
            publish=False, grace_seconds=0, double_build=True)

    def test_official_archives_layers_and_sequences_are_pinned(self):
        self.assertEqual({key: row['bytes']
                          for key, row in ut.ARCHIVE_CONTRACTS.items()}, {
            'map179': 27_317_100, 'ds7': 4_478_185,
            'districts': 36_391_387, 'umos': 5_880_910})
        self.assertEqual(ut.ARCHIVE_CONTRACTS['map179']['layers'],
                         ('Geology_arc', 'Geology_poly'))
        self.assertEqual(ut.ARCHIVE_CONTRACTS['ds7']['layers'][0],
                         'UQFD25_DS7_full')
        for contract in ut.ARCHIVE_CONTRACTS.values():
            self.assertRegex(contract['sha256'], r'^[0-9a-f]{64}$')
            self.assertRegex(
                contract['member_inventory_sha256'], r'^[0-9a-f]{64}$')
        self.assertTrue(all(
            isinstance(value, str) and len(value) == 64
            for value in ut.SOURCE_SEQUENCE_SHA256.values()))

    def test_typed_source_counts_ids_crs_and_scales_are_exact(self):
        expected = {
            'geology_lines': (68_126, 'EPSG:26712'),
            'geology_units': (22_637, 'EPSG:26712'),
            'faults': (19_743, 'EPSG:26912'),
            'districts': (185, 'EPSG:26712'),
            'umos': (7_793, 'EPSG:26912'),
        }
        for key, (count, crs) in expected.items():
            self.assertEqual(
                ut.GEOMETRY_CONTRACTS[key]['source_records'], count)
            self.assertEqual(ut.SOURCE_SPECS[key]['native_crs'], crs)
            self.assertRegex(
                ut.SOURCE_SPECS[key]['manifest_sha256'], r'^[0-9a-f]{64}$')
            self.assertRegex(
                ut.SOURCE_SPECS[key]['source_fids_sha256'], r'^[0-9a-f]{64}$')
        self.assertEqual(ut.TIPPECANOE_MAXZOOM, 12)
        self.assertEqual(ut.TIPPECANOE_FULL_DETAIL, 14)
        self.assertEqual(ut.UT_BOUNDS,
                         (-114.05287, 36.99766, -109.04157, 42.0017))

    def test_geometry_gate_pins_empty_outside_clip_and_unusable(self):
        lines = ut.GEOMETRY_CONTRACTS['geology_lines']
        self.assertEqual(lines['unusable_count'], 2)
        self.assertEqual(lines['outside_count'], 552)
        self.assertEqual(lines['clipped_count'], 520)
        self.assertEqual(lines['output_types'],
                         {'LineString': 67_563, 'MultiLineString': 8})
        units = ut.GEOMETRY_CONTRACTS['geology_units']
        self.assertEqual(units['outside_count'], 2)
        self.assertEqual(units['clipped_count'], 462)
        faults = ut.GEOMETRY_CONTRACTS['faults']
        self.assertEqual(faults['outside_count'], 502)
        self.assertEqual(faults['clipped_count'], 35)
        self.assertEqual(faults['output_types'], {'LineString': 19_232})
        self.assertEqual(ut.GEOMETRY_CONTRACTS['districts']['clipped_count'], 12)
        self.assertEqual(ut.GEOMETRY_CONTRACTS['umos']['empty_count'], 1)
        self.assertEqual(ut.GEOMETRY_CONTRACTS['umos']['outside_count'], 5)
        self.assertTrue(all(row['repair_count'] == 0
                            for row in ut.GEOMETRY_CONTRACTS.values()))

    def test_ds7_z_dimension_is_fully_audited_before_removal(self):
        faults = ut.GEOMETRY_CONTRACTS['faults']
        self.assertEqual(faults['z_count'], 19_743)
        self.assertEqual(faults['z_coordinate_count'], 245_248)
        self.assertEqual(faults['z_zero_coordinate_count'], 245_245)
        self.assertEqual(faults['z_nonzero_coordinate_count'], 3)
        self.assertEqual(
            faults['z_nonzero_fids_sha256'],
            '80e99744819fe75ccb3cfa7c1a032a4b37bb5dee931017c9adcad388e07f74f4')
        for key, contract in ut.GEOMETRY_CONTRACTS.items():
            if key != 'faults':
                self.assertEqual(contract['z_coordinate_count'], 0)

    def test_encoding_exclusions_are_exact_measured_micro_segments(self):
        self.assertEqual(
            [row['fid'] for row in ut.ENCODING_EXCLUSIONS['geology_lines']],
            [22_207])
        self.assertEqual(
            [row['fid'] for row in ut.ENCODING_EXCLUSIONS['faults']],
            [813, 885, 901, 1_615, 2_834, 2_932, 2_967, 5_784, 16_553])
        self.assertFalse(ut.ENCODING_EXCLUSIONS['geology_units'])
        self.assertFalse(ut.ENCODING_EXCLUSIONS['districts'])
        self.assertFalse(ut.ENCODING_EXCLUSIONS['umos'])
        for rows in ut.ENCODING_EXCLUSIONS.values():
            for row in rows:
                self.assertLess(
                    row['output_web_mercator_length_m'],
                    row['z12_full_detail14_web_mercator_unit_m'])
                self.assertEqual(row['source_coordinate_count'], 2)
                self.assertRegex(row['source_geometry_sha256'],
                                 r'^[0-9a-f]{64}$')
                self.assertRegex(row['output_geometry_sha256'],
                                 r'^[0-9a-f]{64}$')
                self.assertIn('maxzoom top-level-ID scan', row['review'])

    def test_safe_zip_extraction_rejects_traversal_and_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            traversal = os.path.join(directory, 'traversal.zip')
            with zipfile.ZipFile(traversal, 'w') as archive:
                archive.writestr('../escape', 'x')
            with self.assertRaisesRegex(RuntimeError, 'unsafe path'):
                ut._safe_extract_archive(
                    'fixture', traversal, os.path.join(directory, 'one'))

            symlink = os.path.join(directory, 'symlink.zip')
            item = zipfile.ZipInfo('link')
            item.create_system = 3
            item.external_attr = (stat.S_IFLNK | 0o777) << 16
            with zipfile.ZipFile(symlink, 'w') as archive:
                archive.writestr(item, 'target')
            with self.assertRaisesRegex(RuntimeError, 'symlink'):
                ut._safe_extract_archive(
                    'fixture', symlink, os.path.join(directory, 'two'))

    def test_tippecanoe_command_is_lossless_and_uses_relative_inputs(self):
        completed = types.SimpleNamespace(returncode=0)
        with tempfile.TemporaryDirectory() as directory:
            output = os.path.join(directory, 'ugs-map179dm-500k.pmtiles')
            units = os.path.join(directory, 'geology_units.geojsonseq')
            lines = os.path.join(directory, 'geology_lines.geojsonseq')
            with mock.patch.object(
                    ut.subprocess, 'run', return_value=completed) as run:
                ut._run_tippecanoe(output, (
                    ('ut_ugs_map179dm_geology', units),
                    ('ut_ugs_map179dm_structures', lines)),
                    ut.ARCHIVE_ATTRIBUTIONS[os.path.basename(output)])
        command = run.call_args.args[0]
        self.assertEqual(run.call_args.kwargs['cwd'],
                         os.path.realpath(directory))
        self.assertIn('--no-feature-limit', command)
        self.assertIn('--no-tile-size-limit', command)
        self.assertIn('--no-tiny-polygon-reduction-at-maximum-zoom', command)
        self.assertIn('--drop-rate=1', command)
        self.assertNotIn('--drop-densest-as-needed', command)
        self.assertEqual(command[-4:], [
            '-L', 'ut_ugs_map179dm_geology:geology_units.geojsonseq',
            '-L', 'ut_ugs_map179dm_structures:geology_lines.geojsonseq'])

    def test_path_free_metadata_allows_authority_slash_but_rejects_temp_path(self):
        path = os.path.join('/private/build', 'ugs-map179dm-500k.pmtiles')
        options = (
            'tippecanoe --output ugs-map179dm-500k.pmtiles '
            '-L ut_ugs_map179dm_geology:geology_units.geojsonseq '
            '-L ut_ugs_map179dm_structures:geology_lines.geojsonseq '
            "'--attribution=Utah Geological Survey; UGS/USGS'")
        stable = {
            'name': 'ut_ugs_map179dm_500k',
            'description': 'ut_ugs_map179dm_500k',
            'attribution': ut.ARCHIVE_ATTRIBUTIONS[os.path.basename(path)],
            'generator': f'tippecanoe {ut.TIPPECANOE_VERSION}',
            'generator_options': options, 'vector_layers': [],
        }
        with mock.patch.object(
                ut, '_pmtiles_json_metadata', return_value=stable):
            self.assertEqual(ut._assert_path_independent_metadata(
                path, 'ut_ugs_map179dm_500k')['status'],
                'complete_path_free_reproducible_metadata')
        changed = dict(stable, generator_options=options + ' /private/build/x')
        with mock.patch.object(
                ut, '_pmtiles_json_metadata', return_value=changed):
            with self.assertRaisesRegex(RuntimeError, 'leaks private paths'):
                ut._assert_path_independent_metadata(
                    path, 'ut_ugs_map179dm_500k')

    @unittest.skipUnless(shutil.which('tippecanoe'), 'tippecanoe unavailable')
    def test_local_scanner_checks_all_properties_and_maxzoom_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            source = os.path.join(directory, 'umos.geojsonseq')
            with open(source, 'w', encoding='utf-8') as output:
                for fid, coordinates in ((7, [-111, 39]), (8, [-110, 40])):
                    json.dump({
                        'type': 'Feature', 'id': fid,
                        'properties': {'fid': fid, 'st': 'UT'},
                        'geometry': {'type': 'Point',
                                     'coordinates': coordinates}}, output)
                    output.write('\n')
            archive = os.path.join(directory, 'ugs-ofr757-umos.pmtiles')
            ut._run_tippecanoe(
                archive, (('ut_ugs_ofr757_umos', source),),
                ut.ARCHIVE_ATTRIBUTIONS[os.path.basename(archive)])
            result = ut._strict_pmtiles_header(
                archive, ['ut_ugs_ofr757_umos'],
                {'ut_ugs_ofr757_umos': ['fid', 'st']},
                verify_feature_properties=True, expected_state='UT',
                expected_bounds=[ut.UT_BOUNDS], collect_feature_ids=True)
            self.assertEqual(
                result['maxzoom_feature_ids']['ut_ugs_ofr757_umos'], [7, 8])
            self.assertGreater(
                result['semantic_layer_counts']['ut_ugs_ofr757_umos'], 2)

    def test_exact_maxzoom_inventory_fails_closed(self):
        stats = {
            'source_records': 3, 'tileable_object_ids': [1, 2, 3],
            'source_object_ids_sha256': ut._canonical_sha256([1, 2, 3]),
        }
        metadata = {
            'maxzoom_feature_ids': {'layer': [1, 2, 3]},
            'maxzoom_feature_instances': {'layer': 4},
        }
        result = ut._source_id_inventory('layer', stats, metadata)
        self.assertEqual(result['unique_maxzoom_ids'], 3)
        metadata['maxzoom_feature_ids']['layer'] = [1, 2]
        with self.assertRaisesRegex(RuntimeError, r'missing=\[3\]'):
            ut._source_id_inventory('layer', stats, metadata)

    def test_browser_descriptors_are_lazy_default_off_and_state_filtered(self):
        stats = {key: {'n': ut.GEOMETRY_CONTRACTS[key]['source_records']}
                 for key in ut.SOURCE_SPECS}
        metadata = {'bounds': list(ut.UT_BOUNDS), 'minzoom': 0,
                    'maxzoom': 12}
        descriptor = ut._browser_descriptor(
            'ut_ugs_map179dm_500k',
            'data/tiles/states/ut/ugs-map179dm-500k.pmtiles',
            metadata, stats)
        self.assertTrue(descriptor['lazy'])
        self.assertFalse(descriptor['default_visible'])
        self.assertEqual(descriptor['state_filter'], ut._state_filter())
        self.assertEqual(len(descriptor['layers']), 2)
        for row in descriptor['layers']:
            self.assertFalse(row['default_visible'])
            self.assertEqual(row['state_filter'], ut._state_filter())
            self.assertEqual(row['style']['filter'], ut._state_filter())
            self.assertTrue(row['required_properties'])
            self.assertEqual(row['bounds'], list(ut.UT_BOUNDS))

    def test_publication_requires_double_build_and_grace(self):
        with mock.patch.object(ut, '_preflight'):
            with self.assertRaisesRegex(RuntimeError, 'double build'):
                ut.build(publish=True, grace_seconds=30, double_build=False)
            with self.assertRaisesRegex(RuntimeError, '30-second grace'):
                ut.build(publish=True, grace_seconds=29, double_build=True)

    def test_manifest_validator_rejects_partial_atomic_set(self):
        manifest = {'national_baselines': {
            'ut_ugs_map179dm_500k': {'status': 'baseline_not_release'}}}
        with self.assertRaisesRegex(RuntimeError, 'must be atomic'):
            ut.validate_manifest_baselines(manifest)

    def _accepted_entry_fixture(self, key):
        accepted = ut.ACCEPTED_ARTIFACT_CONTRACTS[key]
        layers = ut._artifact_layers()[key]
        metadata = {
            'bounds': accepted['bounds'], 'minzoom': 0,
            'maxzoom': ut.TIPPECANOE_MAXZOOM,
            'semantic_layer_counts':
                accepted['semantic_tile_feature_counts'],
            'maxzoom_feature_ids': {
                layer: list(range(count)) for layer, count in
                accepted['maxzoom_unique_feature_ids'].items()},
            'maxzoom_feature_instances': {
                layer: count for layer, count in
                accepted['maxzoom_unique_feature_ids'].items()},
            'field_types': {layer: {'fid': 'Number', 'st': 'String'}
                            for layer in layers},
            'reproducible_metadata': {'status': 'fixture'},
        }
        entry = {
            'schema_version': 1, 'status': 'baseline_not_release',
            'state': 'UT', 'format': 'pmtiles',
            'file': os.path.relpath(ut.BASELINE_KEYS[key], ut.SITE),
            'source': ut._accepted_source_contract(key),
            'retrieved': ut.ACCEPTED_RETRIEVED,
            'n': accepted['n'], 'states': {'UT': accepted['n']},
            'by_layer': {layer: {
                'source_inventory': {}, 'spatial_clip': {},
                'source_id_inventory': {}} for layer in layers},
            'source_id_inventory': {layer: {} for layer in layers},
            'required_properties': {
                layer: ut.LAYER_REQUIREMENTS[layer] for layer in layers},
            'atomic_group': {
                'id': ut.ATOMIC_GROUP_ID,
                'status': 'baseline_not_release',
                'keys': sorted(ut.BASELINE_KEYS),
            },
            'provenance_note': ut.ACCEPTED_PROVENANCE_NOTE,
            'bytes': accepted['bytes'], 'sha256': accepted['sha256'],
            'bounds': accepted['bounds'], 'minzoom': 0,
            'maxzoom': ut.TIPPECANOE_MAXZOOM,
            'field_types': metadata['field_types'],
            'semantic_tile_feature_counts':
                accepted['semantic_tile_feature_counts'],
            'reproducible_metadata': metadata['reproducible_metadata'],
            'deterministic_rebuild': {
                'status': 'two_byte_identical_builds',
                'bytes': accepted['bytes'], 'sha256': accepted['sha256'],
            },
        }
        if len(layers) == 1:
            entry['source_layer'] = layers[0]
        else:
            entry['source_layers'] = list(layers)
        if key == 'ut_ugs_ofr757_umos':
            entry['excluded_source_properties'] = (
                ut.ACCEPTED_UMOS_PROPERTY_EXCLUSIONS)
        source_lookup = ut._source_by_layer()
        browser_stats = {}
        for artifact in ut.ACCEPTED_ARTIFACT_CONTRACTS.values():
            for source_layer, count in artifact[
                    'maxzoom_unique_feature_ids'].items():
                browser_stats[source_lookup[source_layer]] = {'n': count}
        entry['browser_descriptor'] = ut._browser_descriptor(
            key, entry['file'], metadata, browser_stats)
        return entry, metadata

    def test_reviewed_generation_rejects_missing_counts_hashes_and_provenance(self):
        key = 'ut_ugs_ofr757_umos'
        entry, metadata = self._accepted_entry_fixture(key)
        accepted = ut.ACCEPTED_ARTIFACT_CONTRACTS[key]
        path = ut.BASELINE_KEYS[key]
        with mock.patch.object(ut.os.path, 'getsize',
                               return_value=accepted['bytes']), \
                mock.patch.object(ut, '_sha256',
                                  return_value=accepted['sha256']), \
                mock.patch.object(ut, '_validate_source_evidence'):
            self.assertIsNone(
                ut._validate_reviewed_entry(key, entry, path, metadata))
            mutations = []
            missing = copy.deepcopy(entry)
            missing.pop('deterministic_rebuild')
            mutations.append(missing)
            count = copy.deepcopy(entry)
            count['n'] -= 1
            count['states'] = {'UT': count['n']}
            mutations.append(count)
            digest = copy.deepcopy(entry)
            digest['sha256'] = '0' * 64
            digest['deterministic_rebuild']['sha256'] = '0' * 64
            mutations.append(digest)
            provenance = copy.deepcopy(entry)
            provenance['source']['bulk_sha256'] = '0' * 64
            mutations.append(provenance)
            truncated = copy.deepcopy(entry)
            truncated['by_layer'] = {}
            mutations.append(truncated)
            for candidate in mutations:
                with self.subTest(fields=sorted(
                        set(entry) - set(candidate)) or 'changed'):
                    with self.assertRaises(RuntimeError):
                        ut._validate_reviewed_entry(
                            key, candidate, path, metadata)

    def test_reviewed_source_evidence_rejects_sequence_or_geometry_drift(self):
        key = 'ut_ugs_ofr757_umos'
        layer = ut._artifact_layers()[key][0]
        accepted = ut.ACCEPTED_ARTIFACT_CONTRACTS[key]
        metadata = {
            'maxzoom_feature_ids': {
                layer: list(range(accepted[
                    'maxzoom_unique_feature_ids'][layer]))},
            'maxzoom_feature_instances': {
                layer: accepted['maxzoom_unique_feature_ids'][layer]},
        }
        inventory = {}
        row = {'source_inventory': {'source_id_inventory': inventory},
               'spatial_clip': {},
               'source_id_inventory': inventory}
        with self.assertRaisesRegex(RuntimeError, 'source/sequence'):
            ut._validate_source_evidence(
                key, layer, row, inventory, metadata)
        source_key = ut._source_by_layer()[layer]
        geometry = ut.GEOMETRY_CONTRACTS[source_key]
        row['source_inventory'].update({
            'source_id_inventory': inventory,
            'source_records': geometry['source_records'],
            'n': accepted['maxzoom_unique_feature_ids'][layer],
            'source_object_ids_sha256': geometry['source_object_ids_sha256'],
            'tileable_object_ids_sha256': ut._canonical_sha256(
                metadata['maxzoom_feature_ids'][layer]),
            'source_geometry_types': {'Point': geometry['source_records'] - 2},
            'tiled_geometry_types': geometry['output_types'],
            'empty_geometry_count': geometry['empty_count'],
            'empty_geometry_fids_sha256': geometry['empty_sha256'],
            'sequence_bytes': ut.SOURCE_SEQUENCE_BYTES[source_key],
            'sequence_sha256': ut.SOURCE_SEQUENCE_SHA256[source_key],
        })
        with self.assertRaisesRegex(RuntimeError, 'source/sequence'):
            ut._validate_source_evidence(
                key, layer, row, inventory, metadata)

    @unittest.skipUnless(shutil.which('tippecanoe'), 'tippecanoe unavailable')
    def test_real_valid_miniature_four_archive_generation_is_rejected(self):
        """Regression for a 6-feature, four-archive self-consistency bypass."""
        common = {field: 'x' for field in ut.COMMON_PROVENANCE}
        common.update({'fid': 1, 'st': 'UT', 'source_record_id': '1'})
        extras = {
            'ut_ugs_map179dm_geology': {
                'map_unit': 'X', 'unit_name': 'X', 'unit_age': 'X'},
            'ut_ugs_map179dm_structures': {
                'feature_type': 'fault', 'feature_subtype': 'x',
                'location_modifier': 'x'},
            'ut_ugs_ds7_quaternary_faults': {
                'fault_age': 'x', 'mapped_scale': '1:24,000',
                'mapping_constraint': 'x'},
            'ut_ugs_ofr695_mining_districts': {
                'district_name': 'x', 'boundary_status': 'x'},
            'ut_ugs_ofr757_umos': {
                'site_name': 'x', 'commodity': 'gold',
                'occurrence_scope': 'x'},
        }
        geometries = {
            'ut_ugs_map179dm_geology': [{
                'type': 'Polygon', 'coordinates': [[[-112, 39], [-111, 39],
                    [-111, 40], [-112, 40], [-112, 39]]]}],
            'ut_ugs_map179dm_structures': [{
                'type': 'LineString',
                'coordinates': [[-112, 39], [-111, 40]]}],
            'ut_ugs_ds7_quaternary_faults': [{
                'type': 'LineString',
                'coordinates': [[-113, 38], [-110, 41]]}],
            'ut_ugs_ofr695_mining_districts': [{
                'type': 'Polygon', 'coordinates': [[[-112.5, 38.5],
                    [-111.5, 38.5], [-111.5, 39.5], [-112.5, 39.5],
                    [-112.5, 38.5]]]}],
            'ut_ugs_ofr757_umos': [
                {'type': 'Point', 'coordinates': [-111.8, 39.2]},
                {'type': 'Point', 'coordinates': [-110.8, 40.2]}],
        }
        with tempfile.TemporaryDirectory() as directory:
            sequences, stats = {}, {}
            for layer, rows in geometries.items():
                source_key = ut._source_by_layer()[layer]
                sequence = os.path.join(directory, source_key + '.geojsonseq')
                sequences[layer] = sequence
                stats[source_key] = {'n': len(rows)}
                with open(sequence, 'w', encoding='utf-8') as output:
                    for fid, geometry in enumerate(rows, 1):
                        properties = {
                            **common, **extras[layer], 'fid': fid,
                            'source_record_id': str(fid),
                        }
                        json.dump({'type': 'Feature', 'id': fid,
                                   'properties': properties,
                                   'geometry': geometry}, output,
                                  separators=(',', ':'))
                        output.write('\n')
            paths = {key: os.path.join(directory, os.path.basename(path))
                     for key, path in ut.BASELINE_KEYS.items()}
            for key, layers in ut._artifact_layers().items():
                ut._run_tippecanoe(
                    paths[key], tuple((layer, sequences[layer])
                                      for layer in layers),
                    ut.ARCHIVE_ATTRIBUTIONS[os.path.basename(paths[key])])
            metadata = ut._validate_set(paths)
            entries = {}
            for key, path in paths.items():
                layers = ut._artifact_layers()[key]
                observed = metadata[key]
                file = os.path.relpath(path, ut.SITE)
                entry = {
                    'schema_version': 1, 'status': 'baseline_not_release',
                    'state': 'UT', 'format': 'pmtiles', 'file': file,
                    'atomic_group': {
                        'id': ut.ATOMIC_GROUP_ID,
                        'status': 'baseline_not_release',
                        'keys': sorted(paths)},
                    'required_properties': {
                        layer: ut.LAYER_REQUIREMENTS[layer] for layer in layers},
                    'bytes': os.path.getsize(path), 'sha256': ut._sha256(path),
                    'bounds': observed['bounds'],
                    'minzoom': observed['minzoom'],
                    'maxzoom': observed['maxzoom'],
                    'field_types': observed['field_types'],
                    'semantic_tile_feature_counts':
                        observed['semantic_layer_counts'],
                    'source_id_inventory': {layer: {
                        'status': 'complete',
                        'unique_maxzoom_ids': len(
                            observed['maxzoom_feature_ids'][layer]),
                        'maxzoom_object_ids_sha256': ut._canonical_sha256(
                            observed['maxzoom_feature_ids'][layer]),
                        'maxzoom_feature_instances':
                            observed['maxzoom_feature_instances'][layer],
                    } for layer in layers},
                }
                if len(layers) == 1:
                    entry['source_layer'] = layers[0]
                else:
                    entry['source_layers'] = list(layers)
                entry['browser_descriptor'] = ut._browser_descriptor(
                    key, file, observed, stats)
                entries[key] = entry
            with mock.patch.object(ut, 'BASELINE_KEYS', paths):
                with self.assertRaisesRegex(
                        RuntimeError, 'reviewed manifest generation'):
                    ut.validate_manifest_baselines(
                        {'national_baselines': entries})

    def test_atomic_publication_restores_all_files_on_base_exception(self):
        with tempfile.TemporaryDirectory() as directory:
            site = os.path.join(directory, 'site')
            public = os.path.join(site, 'data', 'tiles', 'states', 'ut')
            staging = os.path.join(directory, 'staging')
            os.makedirs(public)
            os.makedirs(staging)
            manifest = os.path.join(site, 'data', 'manifest.json')
            os.makedirs(os.path.dirname(manifest), exist_ok=True)
            with open(manifest, 'w', encoding='utf-8') as output:
                json.dump({'national_baselines': {}, 'sentinel': 1}, output)
            keys = {
                key: os.path.join(public, f'{index}.pmtiles')
                for index, key in enumerate(ut.BASELINE_KEYS)}
            pending, entries = {}, {}
            for key, final in keys.items():
                with open(final, 'wb') as output:
                    output.write(('old-' + key).encode())
                pending[key] = os.path.join(staging, key + '.pmtiles')
                payload = ('new-' + key).encode()
                with open(pending[key], 'wb') as output:
                    output.write(payload)
                entries[key] = {
                    'status': 'baseline_not_release', 'bytes': len(payload),
                    'sha256': ut._sha256(pending[key]),
                }
            original_replace = os.replace

            def interrupt_manifest(source, target):
                if (target == manifest and os.path.basename(source) ==
                        'manifest.json.ut-pending'):
                    raise KeyboardInterrupt('fixture interruption')
                return original_replace(source, target)

            with mock.patch.object(ut, 'SITE', site), \
                    mock.patch.object(ut, 'MANIFEST', manifest), \
                    mock.patch.object(ut, 'OUT_DIR', public), \
                    mock.patch.object(ut, 'BASELINE_KEYS', keys), \
                    mock.patch.object(
                        ut, '_validate_set',
                        return_value={key: {} for key in keys}), \
                    mock.patch.object(ut, '_validate_reviewed_entry'), \
                    mock.patch.object(
                        ut.os, 'replace', side_effect=interrupt_manifest):
                with self.assertRaises(KeyboardInterrupt):
                    ut._publish(pending, entries)
            for key, final in keys.items():
                with open(final, 'rb') as source:
                    self.assertEqual(source.read(), ('old-' + key).encode())
            with open(manifest, encoding='utf-8') as source:
                self.assertEqual(json.load(source),
                                 {'national_baselines': {}, 'sentinel': 1})


if __name__ == '__main__':
    unittest.main()
