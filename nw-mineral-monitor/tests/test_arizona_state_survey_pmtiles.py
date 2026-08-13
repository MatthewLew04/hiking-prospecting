import importlib
import json
import os
import shutil
import stat
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

az = importlib.import_module('build_arizona_state_survey_pmtiles')


def canonical(value):
    return az._canonical_sha256(value)


def point_feature(oid=1, **extra):
    properties = {'OBJECTID': oid, 'State': 'Arizona',
                  'Site_Name': 'Example occurrence'}
    properties.update(extra)
    return {
        'type': 'Feature', 'properties': properties,
        'geometry': {'type': 'Point', 'coordinates': [-112.1, 34.2]},
    }


class ArizonaStateSurveyTests(unittest.TestCase):
    def test_contract_is_three_pmtiles_and_never_an_implicit_publication(self):
        self.assertEqual(set(az.BASELINE_KEYS), {
            'az_azgs_map35_2025', 'az_azgs_mining_districts',
            'az_azgs_critical_minerals'})
        self.assertTrue(all(path.endswith('.pmtiles')
                            for path in az.BASELINE_KEYS.values()))
        self.assertTrue(all('/tiles/states/az/' in path
                            for path in az.BASELINE_KEYS.values()))
        self.assertNotEqual(
            os.path.commonpath((os.path.realpath(az.SITE),
                                os.path.realpath(az.PRIVATE_STAGING_ROOT))),
            os.path.realpath(az.SITE))
        with mock.patch.object(az, 'build') as build:
            az.main([])
        build.assert_called_once_with(publish=False, grace_seconds=0)

    def test_map35_member_and_typed_schema_contracts_are_pinned(self):
        self.assertEqual(az.MAP35_MEMBER_BYTES, 28_155_904)
        self.assertRegex(az.MAP35_MEMBER_SHA256, r'^[0-9a-f]{64}$')
        self.assertEqual(
            az.MAP35_LAYER_CONTRACTS['MapUnitPolys']['n'], 4_841)
        self.assertEqual(
            az.MAP35_LAYER_CONTRACTS['ContactsAndFaults']['n'], 15_563)
        self.assertEqual(
            az.MAP35_LAYER_CONTRACTS['MapUnitPolys']['declared_geometry'],
            'MultiPolygon')
        self.assertEqual(
            az.MAP35_LAYER_CONTRACTS['ContactsAndFaults'][
                'declared_geometry'], 'MultiLineString')
        self.assertEqual(set(az.MAP35_TABLE_CONTRACTS), {
            'DataSources', 'DescriptionOfMapUnits', 'Glossary', 'Symbology'})

    def test_catalog_selection_pins_successor_geopackage(self):
        selected = {
            'collection_id': az.MAP35_COLLECTION_ID,
            'year': '2025',
            'files': [{'name': 'AZStatewide.gpkg', 'type': 'gisdata:layers'}],
            'title': 'Geologic Map of Arizona - 2000', 'series': 'Map-35',
            'authors': [], 'license': {}, 'private': False, 'abstract': 'x',
            'identifiers': {}, 'bounding_box': {}, 'informal_name': 'x',
            'collection_group': {}, 'links': [],
        }
        response = {'data': [{
            'collection_id': selected['collection_id'],
            'metadata': {key: value for key, value in selected.items()
                         if key not in ('collection_id', 'links')},
            'links': [],
        }]}
        self.assertEqual(az._catalog_selection(response), selected)
        response['data'][0]['metadata']['private'] = True
        with self.assertRaisesRegex(RuntimeError, 'identity contract'):
            az._catalog_selection(response)

    def test_dynamic_wrapper_extracts_only_checksum_pinned_member(self):
        with tempfile.TemporaryDirectory() as directory:
            archive_path = os.path.join(directory, 'collection.zip')
            member = 'collection/AZStatewide.gpkg'
            members = {'collection/', member, 'collection/meta.json'}
            payload = b'exact-geopackage'
            with zipfile.ZipFile(archive_path, 'w') as archive:
                archive.writestr('collection/', b'')
                archive.writestr(member, payload)
                archive.writestr('collection/meta.json', b'{}')
            target = os.path.join(directory, 'source.gpkg')
            with mock.patch.object(az, 'MAP35_MEMBER', member), \
                    mock.patch.object(az, 'MAP35_WRAPPER_MEMBERS', members), \
                    mock.patch.object(az, 'MAP35_MEMBER_BYTES', len(payload)), \
                    mock.patch.object(
                        az, 'MAP35_MEMBER_SHA256',
                        __import__('hashlib').sha256(payload).hexdigest()):
                result = az._extract_pinned_geopackage(archive_path, target)
            with open(target, 'rb') as source:
                self.assertEqual(source.read(), payload)
            self.assertEqual(result['member'], member)

            with zipfile.ZipFile(archive_path, 'a') as archive:
                archive.writestr('collection/unreviewed.txt', b'x')
            with mock.patch.object(az, 'MAP35_MEMBER', member), \
                    mock.patch.object(az, 'MAP35_WRAPPER_MEMBERS', members), \
                    mock.patch.object(az, 'MAP35_MEMBER_BYTES', len(payload)), \
                    mock.patch.object(
                        az, 'MAP35_MEMBER_SHA256',
                        __import__('hashlib').sha256(payload).hexdigest()):
                with self.assertRaisesRegex(RuntimeError, 'inventory changed'):
                    az._extract_pinned_geopackage(archive_path, target)

    def test_arcgis_snapshot_requires_typed_oid_metadata_and_exact_ids(self):
        key = 'fixture'
        metadata = {
            'id': 9, 'name': 'Fixture', 'type': 'Feature Layer',
            'serviceItemId': az.AZGS_SERVICE_ITEM_ID,
            'geometryType': 'esriGeometryPoint', 'objectIdField': 'OBJECTID',
            'displayField': 'Name', 'description': '', 'copyrightText': '',
            'maxRecordCount': 1000, 'hasM': False, 'hasZ': False,
            'hasAttachments': False, 'editingInfo': {}, 'extent': {},
            'spatialReference': {'wkid': 4326},
            'fields': [
                {'name': 'OBJECTID', 'type': 'esriFieldTypeOID',
                 'alias': 'OBJECTID', 'length': None, 'nullable': False},
                {'name': 'Name', 'type': 'esriFieldTypeString',
                 'alias': 'Name', 'length': 100, 'nullable': True},
            ],
        }
        selected = az._selected_layer_metadata(metadata)
        ids = [1, 2]
        contract = {
            'url': 'https://example.test/9', 'layer_id': 9,
            'name': 'Fixture', 'geometry_type': 'esriGeometryPoint',
            'oid_field': 'OBJECTID', 'fields': ('OBJECTID', 'Name'),
            'n': 2, 'minimum_object_id': 1, 'maximum_object_id': 2,
            'object_ids_sha256': canonical(ids),
            'layer_metadata_sha256': canonical(selected),
        }
        responses = [metadata, {
            'objectIdFieldName': 'OBJECTID', 'objectIds': [2, 1]}]
        with mock.patch.dict(az.ARCGIS_LAYERS, {key: contract}, clear=True), \
                mock.patch.object(az, '_request_json', side_effect=responses):
            snapshot = az._layer_snapshot(key)
        self.assertEqual(snapshot['ids'], ids)

        metadata['fields'][0]['type'] = 'esriFieldTypeInteger'
        with mock.patch.dict(az.ARCGIS_LAYERS, {key: contract}, clear=True), \
                mock.patch.object(az, '_request_json', side_effect=[
                    metadata, responses[1]]):
            with self.assertRaisesRegex(RuntimeError, 'typed ArcGIS'):
                az._layer_snapshot(key)

    def test_arcgis_pages_are_exact_post_object_id_pages(self):
        key = 'fixture'
        contract = {
            'url': 'https://example.test/0', 'oid_field': 'OBJECTID',
            'fields': ('OBJECTID',),
        }
        snapshot = {'ids': [1, 2, 3]}
        pages = [
            {'features': [point_feature(1), point_feature(2)]},
            {'features': [point_feature(3)]},
        ]
        with mock.patch.dict(az.ARCGIS_LAYERS, {key: contract}, clear=True), \
                mock.patch.object(az, '_request_json', side_effect=pages) as call:
            result = list(az._iter_snapshot(key, snapshot, page=2))
        self.assertEqual(len(result), 3)
        self.assertEqual(call.call_args_list[0].args[1]['objectIds'], '1,2')
        self.assertTrue(call.call_args_list[0].kwargs['post'])

        with mock.patch.dict(az.ARCGIS_LAYERS, {key: contract}, clear=True), \
                mock.patch.object(az, '_request_json', return_value={
                    'features': [point_feature(2), point_feature(1)]}):
            with self.assertRaisesRegex(RuntimeError, 'page mismatch'):
                list(az._iter_snapshot(key, {'ids': [1, 2]}, page=2))

    def test_public_contact_fields_are_not_requested_or_normalized(self):
        fields = az.ARCGIS_LAYERS[
            'az_azgs_critical_minerals']['fields']
        self.assertNotIn('Contact', fields)
        self.assertNotIn('Contact_Email', fields)
        self.assertNotIn('Contact_Phone', fields)
        properties = {key.casefold(): None for key in fields}
        properties.update(objectid=7, site_name='Example')
        normalized = az._normalize_occurrence(7, properties)
        self.assertFalse(any('contact' in key for key in normalized))
        self.assertEqual(normalized['source_scale_status'],
                         'not_applicable_point')

    @unittest.skipIf(az.shapely_shape is None, 'Shapely not installed')
    def test_authoritative_clip_intersects_cross_border_geometry(self):
        clip = az._load_az_clip()
        crossing = az.shapely_shape({
            'type': 'Polygon',
            'coordinates': [[[-109.2, 36.9], [-108.9, 36.9],
                             [-108.9, 37.1], [-109.2, 37.1],
                             [-109.2, 36.9]]],
        })
        result, flags = az._clip_geometry(
            crossing, clip['wgs84'], clip['wgs84_prepared'],
            {'Polygon'}, 'area')
        self.assertTrue(flags['changed'])
        self.assertFalse(flags['outside'])
        self.assertTrue(clip['wgs84'].buffer(1e-9).covers(result))

    def test_encoding_exclusions_pin_geometry_and_do_not_fabricate_lines(self):
        evidence = az._encoding_exclusion_evidence(
            json.loads(json.dumps(
                az.MAP35_FAULT_ENCODING_EXCLUSIONS['records'])))
        self.assertEqual(evidence['count'], 2)
        self.assertEqual(evidence['method'],
                         'no_geometry_fabrication_source_record_omitted')
        changed = json.loads(json.dumps(
            az.MAP35_FAULT_ENCODING_EXCLUSIONS['records']))
        changed[0]['native_length_m'] = 2.0
        with self.assertRaisesRegex(RuntimeError, 'inventory changed'):
            az._encoding_exclusion_evidence(changed)

    def test_tippecanoe_has_no_density_or_tile_size_dropping(self):
        completed = types.SimpleNamespace(returncode=0)
        with mock.patch.object(az.subprocess, 'run', return_value=completed) as run:
            az._run_tippecanoe(
                '/private/out.pmtiles', (('layer', '/private/in.seq'),),
                'AZGS')
        command = run.call_args.args[0]
        self.assertIn('--no-feature-limit', command)
        self.assertIn('--no-tile-size-limit', command)
        self.assertIn('--drop-rate=1', command)
        self.assertIn('--no-tiny-polygon-reduction-at-maximum-zoom', command)
        self.assertNotIn('--drop-densest-as-needed', command)
        self.assertNotIn('--extend-zooms-if-still-dropping', command)
        self.assertNotIn('--read-parallel', command)
        self.assertFalse(any('/private/' in item for item in command))
        self.assertEqual(run.call_args.kwargs['cwd'], '/private')

    @unittest.skipUnless(shutil.which('tippecanoe'), 'tippecanoe is required')
    def test_different_temp_roots_are_byte_identical_and_path_free(self):
        identities = []
        with tempfile.TemporaryDirectory() as first_root, \
                tempfile.TemporaryDirectory() as second_root:
            for root in (first_root, second_root):
                sequence = os.path.join(root, 'fixture.geojsonseq')
                output = os.path.join(root, 'fixture.pmtiles')
                feature = {
                    'type': 'Feature', 'id': 1,
                    'properties': {'fid': 1, 'st': 'AZ'},
                    'geometry': {
                        'type': 'Polygon',
                        'coordinates': [[[-112.1, 34.1], [-112.0, 34.1],
                                         [-112.0, 34.2], [-112.1, 34.1]]],
                    },
                }
                with open(sequence, 'w', encoding='utf-8') as target:
                    target.write(json.dumps(feature, separators=(',', ':')))
                    target.write('\n')
                az._run_tippecanoe(
                    output, (('fixture', sequence),), 'Stable fixture')
                metadata = az._validate_path_free_metadata(
                    output, 'Stable fixture')
                raw_metadata = json.dumps(az._raw_pmtiles_metadata(output))
                self.assertNotIn(root, raw_metadata)
                self.assertNotIn('.staging', raw_metadata)
                identities.append((os.path.getsize(output), az._sha256(output),
                                   metadata['metadata_sha256']))
        self.assertEqual(identities[0], identities[1])

    def test_full_maxzoom_id_inventory_rejects_any_missing_source_id(self):
        metadata = {
            'maxzoom_feature_ids': {'layer': [1, 2, 3]},
            'maxzoom_feature_instances': {'layer': 4},
        }
        result = az._id_inventory(metadata, 'layer', [1, 2, 3])
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(result['maxzoom_unique_tiled_ids'], 3)
        with self.assertRaisesRegex(RuntimeError, 'do not reconcile'):
            az._id_inventory(metadata, 'layer', [1, 2, 3, 4])

    def test_double_build_requires_byte_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            one = os.path.join(directory, 'one.pmtiles')
            two = os.path.join(directory, 'two.pmtiles')

            def same(output, *_args):
                with open(output, 'wb') as target:
                    target.write(b'same')

            with mock.patch.object(az, '_run_tippecanoe', side_effect=same):
                result = az._build_reproducible(
                    one, two, (('layer', 'input'),), 'AZGS')
            self.assertEqual(result['double_build'], 'byte_identical')

            counter = iter((b'one', b'two'))

            def different(output, *_args):
                with open(output, 'wb') as target:
                    target.write(next(counter))

            with mock.patch.object(
                    az, '_run_tippecanoe', side_effect=different):
                with self.assertRaisesRegex(RuntimeError, 'not byte reproducible'):
                    az._build_reproducible(
                        one, two, (('layer', 'input'),), 'AZGS')

    def test_atomic_publication_restores_all_archives_on_base_exception(self):
        with tempfile.TemporaryDirectory() as directory:
            site = os.path.join(directory, 'site')
            public = os.path.join(site, 'data', 'tiles', 'states', 'az')
            staging = os.path.join(directory, 'staging')
            os.makedirs(public)
            os.makedirs(staging)
            manifest = os.path.join(site, 'data', 'manifest.json')
            with open(manifest, 'w', encoding='utf-8') as output:
                json.dump({'national_baselines': {}, 'sentinel': 1}, output)
            keys = {'a': os.path.join(public, 'a.pmtiles'),
                    'b': os.path.join(public, 'b.pmtiles'),
                    'c': os.path.join(public, 'c.pmtiles')}
            pending = {}
            for key, final in keys.items():
                with open(final, 'wb') as output:
                    output.write(('old-' + key).encode())
                pending[key] = os.path.join(staging, key + '.pmtiles')
                with open(pending[key], 'wb') as output:
                    output.write(('new-' + key).encode())
            entries = {key: {'status': 'baseline_not_release'} for key in keys}
            original_replace = os.replace

            def interrupt_manifest(source, target):
                if target == manifest and os.path.basename(source).startswith(
                        '.manifest-az-state-survey-'):
                    raise KeyboardInterrupt('fixture interruption')
                return original_replace(source, target)

            with mock.patch.object(az, 'MANIFEST', manifest), \
                    mock.patch.object(az, 'OUT_DIR', public), \
                    mock.patch.object(az, 'BASELINE_KEYS', keys), \
                    mock.patch.object(az.os, 'replace',
                                      side_effect=interrupt_manifest):
                with self.assertRaises(KeyboardInterrupt):
                    az._publish(pending, entries)
            for key, final in keys.items():
                with open(final, 'rb') as source:
                    self.assertEqual(source.read(), ('old-' + key).encode())
            with open(manifest, encoding='utf-8') as source:
                self.assertEqual(json.load(source)['sentinel'], 1)

    def test_manifest_validation_requires_atomic_three_archive_set(self):
        with self.assertRaisesRegex(RuntimeError, 'atomic set'):
            az.validate_manifest_baselines({
                'national_baselines': {'az_azgs_map35_2025': {}}})
        with self.assertRaisesRegex(RuntimeError, 'absent'):
            az.validate_manifest_baselines({'national_baselines': {}})

    def test_tippecanoe_version_accepts_stderr_contract(self):
        completed = types.SimpleNamespace(
            stdout='', stderr='tippecanoe v2.79.0\n')
        with mock.patch.object(az.subprocess, 'run', return_value=completed):
            self.assertEqual(az._tippecanoe_version(), 'v2.79.0')


if __name__ == '__main__':
    unittest.main()
