import importlib
import json
import os
import sys
import tempfile
import unittest
from unittest import mock


ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), '..'))
PIPELINES = os.path.join(ROOT, 'pipelines')
sys.path.insert(0, PIPELINES)

geophys = importlib.import_module('build_geophys_pmtiles')


def feature(oid, source='airborne', x=-116.0, y=41.0):
    if source == 'airborne':
        properties = {
            'fid': oid, 'name': f'Airborne {oid}', 'state': 'NV',
            'year': 2025, 'mag': 'Y', 'rad': 'N', 'grav': 'N', 'em': 'N',
        }
    else:
        properties = {'OBJECTID': oid, 'PNAME': f'Earth MRI {oid}'}
    return {
        'type': 'Feature', 'properties': properties,
        'geometry': {'type': 'Polygon', 'coordinates': [[
            [x, y], [x + 0.1, y], [x + 0.1, y + 0.1],
            [x, y + 0.1], [x, y],
        ]]},
    }


class GeophysPmtilesTests(unittest.TestCase):
    def test_query_pins_ids_and_reconciles_every_page(self):
        pages = [
            {'objectIdFieldName': 'OBJECTID', 'objectIds': [3, 1, 2]},
            {'features': [feature(2, 'earthmri'), feature(1, 'earthmri')]},
            {'features': [feature(3, 'earthmri')]},
        ]
        with mock.patch.object(geophys, '_request', side_effect=pages) as request:
            rows = geophys._query(
                'https://example.test/layer', 'OBJECTID', ('OBJECTID', 'PNAME'),
                chunk_size=2)
        self.assertEqual([row['properties']['OBJECTID'] for row in rows], [1, 2, 3])
        self.assertEqual(request.call_args_list[0].args[1]['returnIdsOnly'], 'true')
        self.assertEqual(request.call_args_list[1].args[1]['objectIds'], '1,2')
        self.assertEqual(request.call_args_list[2].args[1]['objectIds'], '3')

    def test_query_rejects_snapshot_page_drift_and_duplicate_ids(self):
        cases = [
            ([{'objectIds': [1, 1]}], 'contains duplicates'),
            ([{'objectIds': [1, 2]}, {'features': [feature(1, 'earthmri')]}],
             'snapshot/page mismatch'),
            ([{'objectIds': [1]}, {'features': [
                feature(1, 'earthmri'), feature(1, 'earthmri')]}],
             'duplicate IDs'),
        ]
        for pages, message in cases:
            with self.subTest(message=message), mock.patch.object(
                    geophys, '_request', side_effect=pages):
                with self.assertRaisesRegex(RuntimeError, message):
                    geophys._query('https://example.test/layer', 'OBJECTID',
                                   ('OBJECTID',), chunk_size=2)

    def test_scope_includes_conus_and_both_alaska_longitude_sides_not_hawaii(self):
        self.assertTrue(geophys._in_scope(feature(1, x=-116, y=41)))
        self.assertTrue(geophys._in_scope(feature(1, x=-165, y=61)))
        self.assertTrue(geophys._in_scope(feature(1, x=175, y=52)))
        self.assertFalse(geophys._in_scope(feature(1, x=-157, y=20)))
        self.assertFalse(geophys._in_scope(
            {'type': 'Feature', 'properties': {}, 'geometry': None}))

    def _fixture(self, directory):
        site = os.path.join(directory, 'site')
        staging = os.path.join(directory, 'build-inputs', '.staging')
        output = os.path.join(site, 'data', 'tiles', 'geophys', 'surveys.pmtiles')
        manifest = os.path.join(site, 'data', 'manifest.json')
        os.makedirs(os.path.dirname(output), exist_ok=True)
        with open(manifest, 'w', encoding='utf-8') as target:
            json.dump({'name': 'test', 'ws56': {'keep': {'n': 7}}}, target,
                      separators=(',', ':'))
        return site, staging, output, manifest

    def test_build_semantically_validates_pending_archive_before_atomic_stamp(self):
        with tempfile.TemporaryDirectory() as directory:
            site, staging, output, manifest = self._fixture(directory)

            def fake_tippecanoe(command, check):
                self.assertTrue(check)
                self.assertIn('--no-feature-limit', command)
                self.assertIn('--no-tile-size-limit', command)
                self.assertNotIn('--drop-densest-as-needed', command)
                pending = command[command.index('--output') + 1]
                with open(pending, 'wb') as archive:
                    archive.write(b'PMTiles\x03test-geophysics')

            with mock.patch.object(geophys, 'SITE', site), \
                    mock.patch.object(geophys, 'PRIVATE_STAGING_ROOT', staging), \
                    mock.patch.object(geophys, 'OUT', output), \
                    mock.patch.object(geophys, 'MANIFEST', manifest), \
                    mock.patch.object(geophys.shutil, 'which', return_value='/tippecanoe'), \
                    mock.patch.object(geophys, '_query', side_effect=[
                        [feature(7)], [feature(9, 'earthmri')]]), \
                    mock.patch.object(geophys.subprocess, 'run', side_effect=fake_tippecanoe), \
                    mock.patch.object(geophys, '_pmtiles_header',
                                      return_value={'bytes': 23, 'maxzoom_feature_ids': {
                                          'surveys': [7, 2_000_009]}}) as validate:
                result = geophys.build()

            validate.assert_called_once()
            self.assertEqual(validate.call_args.args[1], ['surveys'])
            self.assertEqual(validate.call_args.args[2], {'surveys': ['src', 'nm']})
            self.assertTrue(validate.call_args.kwargs['verify_feature_properties'])
            self.assertTrue(validate.call_args.kwargs['collect_feature_ids'])
            with open(manifest, encoding='utf-8') as source:
                stamped = json.load(source)
            entry = stamped['ws56']['geophys_surveys']
            self.assertEqual(stamped['ws56']['keep'], {'n': 7})
            self.assertEqual(entry['by_source'], {'airborne': 1, 'earthmri': 1})
            self.assertEqual(entry['bytes'], 23)
            self.assertEqual(entry['source_id_inventory']['source_records'], 2)
            self.assertEqual(entry['source_id_inventory']['maxzoom_unique_tiled_ids'], 2)
            self.assertEqual(len(entry['source_id_inventory']['ids_sha256']), 64)
            self.assertEqual(len(entry['sha256']), 64)
            self.assertEqual(result['features'], 2)
            self.assertTrue(os.path.isfile(output))

    def test_invalid_archive_or_manifest_race_preserves_published_pair(self):
        for failure in ('archive', 'manifest'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                site, staging, output, manifest = self._fixture(directory)
                with open(output, 'wb') as archive:
                    archive.write(b'previous-archive')
                with open(manifest, 'rb') as source:
                    manifest_before = source.read()

                def fake_tippecanoe(command, check):
                    pending = command[command.index('--output') + 1]
                    with open(pending, 'wb') as archive:
                        archive.write(b'new-archive')
                    if failure == 'manifest':
                        with open(manifest, 'ab') as changed:
                            changed.write(b' ')

                validator = (ValueError('invalid PMTiles') if failure == 'archive'
                             else {'bytes': len(b'new-archive'),
                                   'maxzoom_feature_ids': {
                                       'surveys': [7, 2_000_009]}})
                with mock.patch.object(geophys, 'SITE', site), \
                        mock.patch.object(geophys, 'PRIVATE_STAGING_ROOT', staging), \
                        mock.patch.object(geophys, 'OUT', output), \
                        mock.patch.object(geophys, 'MANIFEST', manifest), \
                        mock.patch.object(geophys.shutil, 'which', return_value='/tippecanoe'), \
                        mock.patch.object(geophys, '_query', side_effect=[
                            [feature(7)], [feature(9, 'earthmri')]]), \
                        mock.patch.object(geophys.subprocess, 'run', side_effect=fake_tippecanoe), \
                        mock.patch.object(geophys, '_pmtiles_header', side_effect=(
                            validator if isinstance(validator, Exception) else None),
                            return_value=(validator if isinstance(validator, dict) else None)):
                    with self.assertRaisesRegex(ValueError if failure == 'archive' else RuntimeError,
                                                'invalid PMTiles|manifest changed'):
                        geophys.build()

                with open(output, 'rb') as archive:
                    self.assertEqual(archive.read(), b'previous-archive')
                with open(manifest, 'rb') as source:
                    after = source.read()
                if failure == 'archive':
                    self.assertEqual(after, manifest_before)
                else:
                    self.assertEqual(after, manifest_before + b' ')

    def test_source_id_inventory_rejects_duplicates_and_tiler_omissions(self):
        rows = [geophys._normalize_air(feature(7)),
                geophys._normalize_earthmri(feature(9, 'earthmri'))]
        inventory = geophys._source_id_inventory(rows)
        self.assertEqual(inventory['source_records'], 2)
        self.assertEqual(len(inventory['ids_sha256']), 64)
        rows[1]['properties']['fid'] = 7
        with self.assertRaisesRegex(RuntimeError, 'duplicated'):
            geophys._source_id_inventory(rows)

    def test_keyboard_interrupt_during_manifest_stamp_restores_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            site, staging, output, manifest = self._fixture(directory)
            with open(output, 'wb') as archive:
                archive.write(b'previous-archive')
            with open(manifest, 'rb') as source:
                manifest_before = source.read()

            def fake_tippecanoe(command, check):
                pending = command[command.index('--output') + 1]
                with open(pending, 'wb') as archive:
                    archive.write(b'new-archive')

            real_replace = os.replace
            def interrupt_manifest(source, destination):
                if destination == manifest:
                    raise KeyboardInterrupt('interrupted stamp')
                return real_replace(source, destination)

            with mock.patch.object(geophys, 'SITE', site), \
                    mock.patch.object(geophys, 'PRIVATE_STAGING_ROOT', staging), \
                    mock.patch.object(geophys, 'OUT', output), \
                    mock.patch.object(geophys, 'MANIFEST', manifest), \
                    mock.patch.object(geophys.shutil, 'which', return_value='/tippecanoe'), \
                    mock.patch.object(geophys, '_query', side_effect=[
                        [feature(7)], [feature(9, 'earthmri')]]), \
                    mock.patch.object(geophys.subprocess, 'run', side_effect=fake_tippecanoe), \
                    mock.patch.object(geophys, '_pmtiles_header', return_value={
                        'bytes': len(b'new-archive'),
                        'maxzoom_feature_ids': {'surveys': [7, 2_000_009]}}), \
                    mock.patch.object(geophys.os, 'replace', side_effect=interrupt_manifest):
                with self.assertRaisesRegex(KeyboardInterrupt, 'interrupted stamp'):
                    geophys.build()

            with open(output, 'rb') as archive:
                self.assertEqual(archive.read(), b'previous-archive')
            with open(manifest, 'rb') as source:
                self.assertEqual(source.read(), manifest_before)

    def test_build_rejects_staging_inside_public_site(self):
        with tempfile.TemporaryDirectory() as directory:
            site, _, output, manifest = self._fixture(directory)
            unsafe = os.path.join(site, 'data', 'tiles', '.build')
            with mock.patch.object(geophys, 'SITE', site), \
                    mock.patch.object(geophys, 'PRIVATE_STAGING_ROOT', unsafe), \
                    mock.patch.object(geophys, 'OUT', output), \
                    mock.patch.object(geophys, 'MANIFEST', manifest), \
                    mock.patch.object(geophys.shutil, 'which', return_value='/tippecanoe'), \
                    mock.patch.object(geophys, '_query', side_effect=[
                        [feature(7)], [feature(9, 'earthmri')]]):
                with self.assertRaisesRegex(RuntimeError, 'outside public site'):
                    geophys.build()
            self.assertFalse(os.path.exists(unsafe),
                             'fail-closed preflight must not create public staging')


if __name__ == '__main__':
    unittest.main()
