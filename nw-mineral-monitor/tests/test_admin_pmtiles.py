import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from unittest import mock


ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.join(ROOT, 'pipelines'))

import build_admin_pmtiles as admin
import validate_national


def _square(seed=0):
    west = -120 + seed * 0.01
    south = 35 + seed * 0.01
    return {
        'type': 'Polygon',
        'coordinates': [[
            [west, south], [west + .005, south],
            [west + .005, south + .005], [west, south + .005],
            [west, south],
        ]],
    }


def _state(fips='01', code='AL', name='Alabama', seed=0):
    return {
        'type': 'Feature',
        'properties': {'GEOID': fips, 'STUSAB': code, 'NAME': name},
        'geometry': _square(seed),
    }


def _county(fips='01001', state='01', name='Autauga County', seed=0):
    return {
        'type': 'Feature',
        'properties': {'GEOID': fips, 'STATE': state, 'NAME': name},
        'geometry': _square(seed),
    }


def _ids_hash(values):
    raw = json.dumps(sorted(values), separators=(',', ':')).encode('ascii')
    return hashlib.sha256(raw).hexdigest()


class AdminSnapshotContractTests(unittest.TestCase):
    @staticmethod
    def snapshot_document():
        features = [_state()]
        records_sha256 = hashlib.sha256(
            admin._canonical_bytes(features)).hexdigest()
        metadata_sha256 = 'a' * 64
        object_ids_sha256 = 'b' * 64
        source = admin.SERVICE + '/0'
        source_snapshot_id = hashlib.sha256(admin._canonical_bytes({
            'layer': 'states', 'source': source,
            'metadata_sha256': metadata_sha256,
            'object_ids_sha256': object_ids_sha256,
            'records_sha256': records_sha256,
        })).hexdigest()
        return {
            'schema_version': 1, 'system': admin.SYSTEM,
            'vintage': admin.VINTAGE, 'layer': 'states', 'source': source,
            'retrieved': '2026-08-13', 'complete': True, 'truncated': False,
            'pagination': {
                'method': admin.SNAPSHOT_CONTRACT,
                'source_count': 1, 'fetched_count': 1,
                'object_id_field': 'OBJECTID',
                'object_ids_sha256': object_ids_sha256,
                'metadata_sha256': metadata_sha256,
                'records_sha256': records_sha256,
                'page_size': 500, 'page_count': 1,
                'full_second_feature_pass': True,
                'postflight_metadata_match': True,
                'postflight_object_ids_match': True,
                'exceeded_transfer_limit': False,
                'source_snapshot_id': source_snapshot_id,
            },
            'query': admin._snapshot_query('states'),
            'type': 'FeatureCollection', 'features': features,
        }

    def test_snapshot_requires_exact_object_id_double_pass_evidence(self):
        document = self.snapshot_document()
        with mock.patch.dict(admin.EXPECTED_COUNTS,
                             {'states': 1, 'counties': 1}, clear=True):
            self.assertEqual(
                admin._validate_snapshot_header(document, 'states'),
                document['features'])
            for field, value in (
                    ('full_second_feature_pass', False),
                    ('postflight_metadata_match', False),
                    ('postflight_object_ids_match', False),
                    ('exceeded_transfer_limit', True),
                    ('records_sha256', '0' * 64),
                    ('source_snapshot_id', '0' * 64)):
                bad = json.loads(json.dumps(document))
                bad['pagination'][field] = value
                with self.subTest(field=field), self.assertRaises(
                        admin.PublicationError):
                    admin._validate_snapshot_header(bad, 'states')

    def test_current_accepted_identity_is_pinned(self):
        self.assertEqual(admin.CURRENT_ACCEPTED_ARTIFACT, {
            'bytes': 7_743_967,
            'sha256': '94c3a78b2ca17f02223e6d5161afde763a370b515e710723e76395b520e2c3df',
        })
        self.assertEqual(admin.PREVIOUS_PATH_DEPENDENT_ARTIFACT, {
            'bytes': 7_895_670,
            'sha256': '4aba83f4929ab7c04ccd3c6b0a9d938d45c9ab082789479ba70d01c6d6c446aa',
        })
        self.assertEqual(admin.EXPECTED_COUNTS,
                         {'states': 49, 'counties': 3_138})
        self.assertEqual(
            admin.EXPECTED_FIPS_IDS_SHA256['states'],
            '155b69af91d4816940212a1ab613d9afaf6dd3219eaa9bd1ef63037ba1bcaef4')
        self.assertEqual(
            admin.EXPECTED_FIPS_IDS_SHA256['counties'],
            'a37a3c2581375c33746a4fe50ab907b9fdde986521113b9f508d4fb155b48da1')
        self.assertEqual(admin.EXPECTED_BOUNDS,
                         [-179.23109, 24.39631, 179.85968, 71.43979])
        self.assertEqual(admin.CURRENT_ACCEPTED_STATE_CLIPS, {
            'bytes': 707_923,
            'sha256':
                '33c09d367d74a1ce0c88934d4adb548557733bf7da9105be039f5f16ed22c552',
        })

    def test_fresh_geometries_reproduce_reviewed_state_clip_bytes(self):
        with open(os.path.join(ROOT, 'infra', 'state_clips.json'),
                  encoding='utf-8') as source:
            current = json.load(source)
        document, raw = admin._state_clip_bytes(current['states'])
        self.assertEqual(list(document['states']), list(admin.STATE_CLIP_ORDER))
        self.assertEqual(len(raw), admin.CURRENT_ACCEPTED_STATE_CLIPS['bytes'])
        self.assertEqual(hashlib.sha256(raw).hexdigest(),
                         admin.CURRENT_ACCEPTED_STATE_CLIPS['sha256'])
        with open(os.path.join(ROOT, 'infra', 'state_clips.json'), 'rb') as source:
            self.assertEqual(raw, source.read())

    def test_previous_legacy_artifact_is_not_claimed_as_builder_output(self):
        # The old checksum is an audit anchor only. Descriptor generation takes
        # the checksum of the newly compared candidate builds as an argument.
        source = admin._descriptor.__code__.co_varnames
        self.assertIn('artifact', source)
        candidate = {'bytes': 12, 'sha256': 'f' * 64}
        descriptor = admin._descriptor({
            'retrieved': '2026-08-13', 'clip_bytes': b'{}',
            'inventory_sha256': 'e' * 64,
            'integrity': {
                'states': {'bytes': 1, 'sha256': 'a' * 64},
                'counties': {'bytes': 2, 'sha256': 'b' * 64},
            },
            'source_snapshot': {
                layer: {
                    'source_snapshot_id': 'c' * 64,
                    'metadata_sha256': 'd' * 64,
                    'object_ids_sha256': 'e' * 64,
                    'records_sha256': 'f' * 64,
                } for layer in ('states', 'counties')
            }}, candidate,
            {'states': {}, 'counties': {}}, {'status': 'complete'})
        self.assertEqual(descriptor['bytes'], 12)
        self.assertEqual(descriptor['sha256'], 'f' * 64)
        self.assertEqual(descriptor['deterministic_rebuild']['sha256'],
                         'f' * 64)
        self.assertNotEqual(descriptor['sha256'],
                            admin.PREVIOUS_PATH_DEPENDENT_ARTIFACT['sha256'])

    def test_wrong_duplicate_and_missing_state_fips_fail_closed(self):
        identities = {'01': ('AL', 'Alabama'), '02': ('AK', 'Alaska')}
        digest = _ids_hash([1, 2])
        valid = [_state(), _state('02', 'AK', 'Alaska', 1)]
        normalized = admin._normalize_features(
            valid, 'states', state_identities=identities,
            expected_count=2, expected_ids_sha256=digest)
        self.assertEqual([row['id'] for row in normalized], [1, 2])

        cases = {
            'wrong identity': [_state(code='AK'), valid[1]],
            'unexpected FIPS': [_state(), _state('03', 'AK', 'Alaska', 1)],
            'duplicate FIPS': [_state(), _state(seed=1)],
            'missing FIPS': [_state()],
        }
        for label, features in cases.items():
            with self.subTest(label=label), self.assertRaises(admin.PublicationError):
                admin._normalize_features(
                    features, 'states', state_identities=identities,
                    expected_count=2, expected_ids_sha256=digest)

    def test_wrong_duplicate_and_missing_county_fips_fail_closed(self):
        identities = {'01': ('AL', 'Alabama'), '02': ('AK', 'Alaska')}
        digest = _ids_hash([1001, 2020])
        valid = [_county(), _county('02020', '02', 'Anchorage Municipality', 1)]
        normalized = admin._normalize_features(
            valid, 'counties', state_identities=identities,
            expected_count=2, expected_ids_sha256=digest)
        self.assertEqual([row['id'] for row in normalized], [1001, 2020])

        cases = {
            'wrong prefix': [_county(state='02'), valid[1]],
            'duplicate': [_county(), _county(seed=1)],
            'missing': [_county()],
        }
        for label, features in cases.items():
            with self.subTest(label=label), self.assertRaises(admin.PublicationError):
                admin._normalize_features(
                    features, 'counties', state_identities=identities,
                    expected_count=2, expected_ids_sha256=digest)

    def test_inventory_byte_hash_count_and_id_hash_drift_fail(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'states.geojson')
            raw = b'{"private":"snapshot"}'
            with open(path, 'wb') as output:
                output.write(raw)
            entry = {
                'file': 'states.geojson',
                'bytes': len(raw),
                'sha256': hashlib.sha256(raw).hexdigest(),
                'count': 49,
                'fips_ids_sha256': admin.EXPECTED_FIPS_IDS_SHA256['states'],
                'properties_sha256':
                    admin.EXPECTED_PROPERTIES_SHA256['states'],
            }
            self.assertEqual(
                admin._validate_inventory_entry(entry, 'states', path),
                {'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()})
            mutations = (
                {'bytes': len(raw) + 1},
                {'sha256': '0' * 64},
                {'count': 48},
                {'fips_ids_sha256': '0' * 64},
                {'properties_sha256': '0' * 64},
            )
            for change in mutations:
                bad = dict(entry)
                bad.update(change)
                with self.subTest(change=change), self.assertRaises(
                        admin.PublicationError):
                    admin._validate_inventory_entry(bad, 'states', path)


class AdminTileContractTests(unittest.TestCase):
    @staticmethod
    def _context():
        states = admin._normalize_features(
            [_state()], 'states', state_identities={'01': ('AL', 'Alabama')},
            expected_count=1, expected_ids_sha256=_ids_hash([1]))
        counties = admin._normalize_features(
            [_county()], 'counties',
            state_identities={'01': ('AL', 'Alabama')},
            expected_count=1, expected_ids_sha256=_ids_hash([1001]))
        return {'normalized': {'states': states, 'counties': counties}}

    def test_metadata_only_property_claim_does_not_pass_full_scan(self):
        context = self._context()

        def metadata_only_header(path, layers, required, **kwargs):
            self.assertTrue(kwargs['verify_feature_properties'])
            self.assertTrue(kwargs['collect_feature_ids'])
            # Metadata says all fields exist, but this individual feature omits
            # ``name``. The builder-owned semantic callback must still reject it.
            kwargs['feature_validator']('states', {
                'id': 1,
                'properties': {'fips': '01', 'st': 'AL'},
                'geometry_type': 3,
            }, True)
            raise AssertionError('missing feature property was accepted')

        with mock.patch.dict(admin.EXPECTED_COUNTS,
                             {'states': 1, 'counties': 1}, clear=True), \
                mock.patch.dict(admin.EXPECTED_FIPS_IDS_SHA256, {
                    'states': _ids_hash([1]), 'counties': _ids_hash([1001])},
                    clear=True), \
                self.assertRaises(admin.PublicationError):
            admin._validate_pmtiles(
                'metadata-only.pmtiles', context, '{}',
                pmtiles_header=metadata_only_header)

    def test_full_scan_flags_and_exact_maxzoom_ids_are_required(self):
        context = self._context()
        calls = {}

        def full_header(path, layers, required, **kwargs):
            calls.update(kwargs)
            validator = kwargs['feature_validator']
            validator('states', {
                'id': 1,
                'properties': {'fips': '01', 'name': 'Alabama', 'st': 'AL'},
                'geometry_type': 3,
            }, True)
            validator('counties', {
                'id': 1001,
                'properties': {
                    'fips': '01001', 'name': 'Autauga County', 'st': 'AL'},
                'geometry_type': 3,
            }, True)
            return {
                'source_layers': ['counties', 'states'],
                'minzoom': 0, 'maxzoom': 10,
                'bounds': list(admin.EXPECTED_BOUNDS),
                'field_types': {
                    'counties': {'fips': 'String', 'name': 'String', 'st': 'String'},
                    'states': {'fips': 'String', 'name': 'String', 'st': 'String'},
                },
                'maxzoom_feature_ids': {'states': [1], 'counties': [1001]},
                'maxzoom_feature_instances': {'states': 2, 'counties': 3},
            }

        with mock.patch.dict(admin.EXPECTED_COUNTS,
                             {'states': 1, 'counties': 1}, clear=True), \
                mock.patch.dict(admin.EXPECTED_FIPS_IDS_SHA256, {
                    'states': _ids_hash([1]), 'counties': _ids_hash([1001])},
                    clear=True), \
                mock.patch.object(admin, '_path_free_metadata',
                                  return_value={'status': 'ok'}):
            _, inventory, reproducible = admin._validate_pmtiles(
                'synthetic.pmtiles', context, '{}', pmtiles_header=full_header)
        self.assertTrue(calls['verify_feature_properties'])
        self.assertTrue(calls['collect_feature_ids'])
        self.assertEqual(calls['expected_geometry_types'],
                         {'states': {3}, 'counties': {3}})
        self.assertEqual(inventory['states']['maxzoom_unique_tiled_ids'], 1)
        self.assertEqual(inventory['counties']['maxzoom_feature_instances'], 3)
        self.assertRegex(inventory['states']['properties_sha256'],
                         r'^[0-9a-f]{64}$')
        self.assertNotEqual(inventory['states']['properties_sha256'],
                            inventory['counties']['properties_sha256'])
        self.assertEqual(reproducible, {'status': 'ok'})

    def test_tippecanoe_contract_is_path_free_and_double_build_is_exact(self):
        command = admin._tippecanoe_command('{}')
        self.assertIn('--output=admin.pmtiles', command)
        self.assertIn('states:states.geojsonseq', command)
        self.assertIn('counties:counties.geojsonseq', command)
        self.assertTrue(all('/' not in item and '\\' not in item
                            for item in command))
        self.assertIn('--no-feature-limit', command)
        self.assertIn('--no-tile-size-limit', command)

        with tempfile.TemporaryDirectory() as directory:
            first = os.path.join(directory, 'first.pmtiles')
            second = os.path.join(directory, 'second.pmtiles')
            for path in (first, second):
                with open(path, 'wb') as output:
                    output.write(b'same bytes')
            exact = admin._assert_identical_builds(first, second)
            self.assertEqual(exact['bytes'], 10)
            with open(second, 'ab') as output:
                output.write(b' changed')
            with self.assertRaises(admin.PublicationError):
                admin._assert_identical_builds(first, second)


class AdminPublicationTransactionTests(unittest.TestCase):
    def test_publication_lock_is_private_and_ignored(self):
        expected_parent = os.path.realpath(os.path.join(
            admin.ROOT, 'build-inputs', '.staging'))
        self.assertEqual(os.path.realpath(os.path.dirname(admin.PUBLICATION_LOCK)),
                         expected_parent)
        self.assertNotEqual(os.path.dirname(admin.PUBLICATION_LOCK), admin.ROOT)

    def test_publication_creates_private_lock_parent(self):
        with tempfile.TemporaryDirectory() as root:
            admin_out = os.path.join(root, 'site', 'admin.pmtiles')
            clips_out = os.path.join(root, 'infra', 'state_clips.json')
            manifest = os.path.join(root, 'site', 'manifest.json')
            lock = os.path.join(
                root, 'build-inputs', '.staging', '.admin-publication.lock')
            os.makedirs(os.path.dirname(manifest), exist_ok=True)
            with open(manifest, 'wb') as output:
                output.write(admin._manifest_bytes({
                    'national_baselines': {'keep': {'v': 1}},
                    'sources': {'boundaries': 'old', 'keep': 'yes'},
                }))
            pending_admin = os.path.join(root, 'pending-admin')
            pending_clips = os.path.join(root, 'pending-clips')
            artifact, clips = b'new admin', b'new clips'
            for path, raw in ((pending_admin, artifact),
                              (pending_clips, clips)):
                with open(path, 'wb') as output:
                    output.write(raw)
            descriptor = {
                'bytes': len(artifact),
                'sha256': hashlib.sha256(artifact).hexdigest(),
                'state_clips': {
                    'bytes': len(clips),
                    'sha256': hashlib.sha256(clips).hexdigest()},
            }
            with mock.patch.object(admin, 'MANIFEST', manifest), \
                    mock.patch.object(admin, 'PUBLICATION_LOCK', lock):
                admin._publish_bundle(
                    pending_admin, pending_clips, descriptor,
                    admin_out=admin_out, clips_out=clips_out,
                    manifest_path=manifest)
            self.assertTrue(os.path.isfile(lock))
            self.assertEqual(os.path.getsize(lock), 0)
            with open(manifest, encoding='utf-8') as source:
                published = json.load(source)
            self.assertEqual(published['national_baselines']['keep'], {'v': 1})
            self.assertEqual(published['national_baselines']['admin'], descriptor)
            self.assertEqual(published['sources'], {
                'boundaries': admin.BOUNDARIES_SOURCE, 'keep': 'yes'})

    def test_cli_defaults_cannot_select_public_outputs(self):
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                admin.main(['--staging-dir', '/private/source'])
        self.assertEqual(raised.exception.code, 2)

    def test_private_output_rejects_the_public_site_tree(self):
        for path in (os.path.join(admin.SITE, 'data', 'candidate'),
                     os.path.abspath(os.sep), admin.ROOT,
                     os.path.expanduser('~')):
            with self.subTest(path=path), self.assertRaises(
                    admin.PublicationError):
                admin._private_output(path)

    def test_independent_temp_roots_receive_identical_path_free_commands(self):
        calls = []

        def fake_run(command, cwd, env, check):
            calls.append((list(command), cwd, dict(env)))
            with open(os.path.join(cwd, 'admin.pmtiles'), 'wb') as output:
                # A deterministic stand-in for Tippecanoe: bytes depend only on
                # the relative command and normalized input bytes, not temp root.
                digest = hashlib.sha256()
                digest.update(json.dumps(command, separators=(',', ':')).encode())
                for name in ('states.geojsonseq', 'counties.geojsonseq'):
                    with open(os.path.join(cwd, name), 'rb') as source:
                        digest.update(source.read())
                output.write(digest.digest())
            return mock.Mock(returncode=0)

        sequences = {'states': b'states\n', 'counties': b'counties\n'}
        with tempfile.TemporaryDirectory() as first_root, \
                tempfile.TemporaryDirectory() as second_root, \
                mock.patch.object(admin.subprocess, 'run', side_effect=fake_run):
            first = admin._run_tippecanoe(first_root, sequences, '{}')
            second = admin._run_tippecanoe(second_root, sequences, '{}')
            with open(first, 'rb') as source:
                first_bytes = source.read()
            with open(second, 'rb') as source:
                second_bytes = source.read()
        self.assertEqual(first_bytes, second_bytes)
        self.assertEqual(calls[0][0], calls[1][0])
        metadata = {
            'name': 'admin.pmtiles', 'description': '{}',
            'attribution': admin.ATTRIBUTION,
            'generator': 'tippecanoe v2.79.0',
            'generator_options': ' '.join(calls[0][0]),
        }
        with mock.patch.object(admin, '_read_pmtiles_metadata',
                              return_value=metadata):
            first_metadata = admin._path_free_metadata(first, '{}')
            second_metadata = admin._path_free_metadata(second, '{}')
        self.assertEqual(first_metadata, second_metadata)
        for command, _, environment in calls:
            self.assertTrue(all('/' not in item and '\\' not in item
                                for item in command))
            self.assertEqual(environment['TIPPECANOE_MAX_THREADS'], '1')
            self.assertEqual(environment['LC_ALL'], 'C')
            self.assertEqual(environment['TZ'], 'UTC')

    def test_baseexception_rolls_back_artifact_clips_and_manifest(self):
        with tempfile.TemporaryDirectory() as root:
            admin_out = os.path.join(root, 'site', 'admin.pmtiles')
            clips_out = os.path.join(root, 'infra', 'state_clips.json')
            manifest = os.path.join(root, 'site', 'manifest.json')
            os.makedirs(os.path.dirname(admin_out), exist_ok=True)
            os.makedirs(os.path.dirname(clips_out), exist_ok=True)
            old = {
                admin_out: b'old admin',
                clips_out: b'old clips',
                manifest: (
                    b'{"national_baselines":{"keep":{"v":1}},'
                    b'"sources":{"boundaries":"old source","keep":"yes"}}'),
            }
            for path, raw in old.items():
                with open(path, 'wb') as output:
                    output.write(raw)
            admin_pending = os.path.join(root, 'pending-admin')
            clips_pending = os.path.join(root, 'pending-clips')
            new_admin = b'new admin'
            new_clips = b'new clips'
            for path, raw in ((admin_pending, new_admin),
                              (clips_pending, new_clips)):
                with open(path, 'wb') as output:
                    output.write(raw)
            descriptor = {
                'bytes': len(new_admin),
                'sha256': hashlib.sha256(new_admin).hexdigest(),
                'state_clips': {
                    'bytes': len(new_clips),
                    'sha256': hashlib.sha256(new_clips).hexdigest()},
            }

            def interrupted_merge(value, path):
                with open(path, 'w', encoding='utf-8') as output:
                    json.dump({
                        'national_baselines': {
                            'keep': {'v': 1}, 'admin': value},
                        'sources': {
                            'boundaries': admin.BOUNDARIES_SOURCE,
                            'keep': 'yes'},
                    }, output)
                raise KeyboardInterrupt('synthetic interrupt')

            with self.assertRaises(KeyboardInterrupt):
                admin._publish_bundle(
                    admin_pending, clips_pending, descriptor,
                    admin_out=admin_out, clips_out=clips_out,
                    manifest_path=manifest, manifest_merge=interrupted_merge)
            for path, raw in old.items():
                with self.subTest(path=path):
                    with open(path, 'rb') as source:
                        self.assertEqual(source.read(), raw)
            with open(manifest, encoding='utf-8') as source:
                rolled_back = json.load(source)
            self.assertEqual(rolled_back['sources'],
                             {'boundaries': 'old source', 'keep': 'yes'})
            self.assertFalse(os.path.exists(admin_pending))
            self.assertFalse(os.path.exists(clips_pending))

    def test_manifest_merge_preserves_unrelated_latest_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'manifest.json')
            original = {
                'build': 'newest',
                'national_baselines': {'mrds': {'sha256': 'a' * 64}},
                'sources': {'boundaries': 'stale source', 'keep': 'yes'},
                'unrelated': {'keep': True},
            }
            with open(path, 'wb') as output:
                output.write(admin._manifest_bytes(original))
            descriptor = {'schema_version': 1, 'file': 'admin.pmtiles'}
            admin._merge_admin_manifest(descriptor, path)
            with open(path, encoding='utf-8') as source:
                merged = json.load(source)
            self.assertEqual(merged['build'], 'newest')
            self.assertEqual(merged['unrelated'], {'keep': True})
            self.assertEqual(merged['national_baselines']['mrds'],
                             {'sha256': 'a' * 64})
            self.assertEqual(merged['national_baselines']['admin'], descriptor)
            self.assertEqual(merged['sources'], {
                'boundaries': admin.BOUNDARIES_SOURCE, 'keep': 'yes'})
            with open(path, 'rb') as source:
                self.assertEqual(source.read(), admin._manifest_bytes(merged))

    def test_manifest_merge_rejects_noncanonical_latest_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'manifest.json')
            original = {
                'national_baselines': {},
                'sources': {'boundaries': 'stale source'},
            }
            with open(path, 'w', encoding='utf-8') as output:
                json.dump(original, output, indent=2)
            with self.assertRaisesRegex(
                    admin.PublicationError, 'reconcile-canonical'):
                admin._merge_admin_manifest(
                    {'schema_version': 1, 'file': 'admin.pmtiles'}, path)


class AdminPublicValidatorTests(unittest.TestCase):
    @staticmethod
    def _manifest():
        return {
            'sources': {'boundaries': validate_national.ADMIN_BOUNDARIES_SOURCE},
            'national_baselines': {
                'admin': validate_national._admin_expected_descriptor()},
        }

    @staticmethod
    def _metadata():
        # Tests patch the two independently pinned hashes. Structural/path-free
        # checks remain live here and are exercised separately below.
        return {
            'name': 'admin.pmtiles',
            'description': validate_national._admin_expected_description(),
            'attribution': validate_national.ADMIN_ATTRIBUTION,
            'generator': 'tippecanoe v2.79.0',
            'generator_options': 'tippecanoe --output=admin.pmtiles',
        }

    @staticmethod
    def _valid_scan(path, layers, properties, **kwargs):
        validator = kwargs['feature_validator']
        states = {
            int(fips): (fips, f'State {fips}', state)
            for fips, state in validate_national.ADMIN_STATE_FIPS.items()}
        # Fill synthetic county rows, then tests patch exact independently
        # pinned inventory hashes/counts to these small fixtures.
        counties = {
            int(f'{fips}001'): (f'{fips}001', f'County {fips}', state)
            for fips, state in validate_national.ADMIN_STATE_FIPS.items()}
        for layer, rows in (('states', states), ('counties', counties)):
            for feature_id, (fips, name, state) in rows.items():
                validator(layer, {'id': feature_id, 'properties': {
                    'fips': fips, 'name': name, 'st': state},
                    'geometry_type': 3}, True)
        return {
            'source_layers': ['counties', 'states'],
            'minzoom': 0, 'maxzoom': 10,
            'bounds': list(validate_national.ADMIN_BOUNDS),
            'field_types': {
                'counties': {'fips': 'String', 'name': 'String', 'st': 'String'},
                'states': {'fips': 'String', 'name': 'String', 'st': 'String'},
            },
            'maxzoom_feature_ids': {
                'states': sorted(states), 'counties': sorted(counties)},
            'maxzoom_feature_instances': {
                'states': len(states), 'counties': len(counties)},
        }

    def _run(self, manifest, *, header=None, metadata=None,
             artifact_sha=None, clip_sha=None):
        qa = validate_national.QA()
        manifest = json.loads(json.dumps(manifest))
        accepted_before_patch = validate_national._admin_expected_descriptor()
        metadata = self._metadata() if metadata is None else metadata
        metadata_hash = hashlib.sha256(
            validate_national._canonical_json_bytes(metadata)).hexdigest()
        options_hash = hashlib.sha256(validate_national._canonical_json_bytes(
            metadata.get('generator_options'))).hexdigest()
        candidate = ((manifest.get('national_baselines') or {}).get('admin')
                     if isinstance(manifest, dict) else None)
        if candidate == accepted_before_patch:
            candidate['reproducible_metadata']['metadata_sha256'] = metadata_hash
            candidate['reproducible_metadata'][
                'generator_options_sha256'] = options_hash
        clips = {
            'schema_version': 1,
            'source': (validate_national.ADMIN_SOURCE +
                       '/0 (States) and /1 (Counties), January 1 2025 vintage'),
            'note': 'Build-side/Lambda spatial clips only.',
            'states': {code: _square(index)
                       for index, code in enumerate(
                           validate_national.ALL_STATES)},
        }
        with mock.patch.object(validate_national.os.path, 'isfile',
                               return_value=True), \
                mock.patch.object(validate_national.os.path, 'getsize',
                                  side_effect=[
                                      validate_national.ADMIN_ARTIFACT_BYTES,
                                      validate_national.ADMIN_STATE_CLIPS_BYTES]), \
                mock.patch.object(validate_national, '_sha256_file',
                                  side_effect=[
                                      artifact_sha or
                                      validate_national.ADMIN_ARTIFACT_SHA256,
                                      clip_sha or
                                      validate_national.ADMIN_STATE_CLIPS_SHA256]), \
                mock.patch.object(validate_national, '_load_json',
                                  return_value=clips), \
                mock.patch.object(validate_national, '_admin_pmtiles_metadata',
                                  return_value=metadata), \
                mock.patch.object(validate_national,
                                  'ADMIN_METADATA_SHA256', metadata_hash), \
                mock.patch.object(validate_national,
                                  'ADMIN_GENERATOR_OPTIONS_SHA256', options_hash):
            validate_national.validate_admin_baseline(
                qa, manifest, pmtiles_header=header or self._valid_scan)
        return qa

    def test_missing_and_self_consistent_forged_descriptors_fail_exact_pin(self):
        missing = self._manifest()
        missing['national_baselines'].pop('admin')
        self.assertTrue(any('descriptor is missing' in error
                            for error in self._run(missing).errors))

        forged = self._manifest()
        descriptor = forged['national_baselines']['admin']
        descriptor['bytes'] = 1
        descriptor['sha256'] = 'f' * 64
        descriptor['deterministic_rebuild']['bytes'] = 1
        descriptor['deterministic_rebuild']['sha256'] = 'f' * 64
        self.assertTrue(any('descriptor is missing' in error
                            for error in self._run(forged).errors))

        wrong_type = self._manifest()
        wrong_type['national_baselines']['admin']['schema_version'] = True
        self.assertTrue(any('descriptor is missing' in error
                            for error in self._run(wrong_type).errors))

    def test_exact_descriptor_and_full_semantic_scan_can_pass(self):
        rows = {
            'states': {
                int(fips): (fips, f'State {fips}', state)
                for fips, state in validate_national.ADMIN_STATE_FIPS.items()},
            'counties': {
                int(f'{fips}001'): (f'{fips}001', f'County {fips}', state)
                for fips, state in validate_national.ADMIN_STATE_FIPS.items()},
        }
        counts = {layer: len(values) for layer, values in rows.items()}
        id_hashes = {
            layer: validate_national._admin_ids_sha256(values)
            for layer, values in rows.items()}
        property_hashes = {
            layer: validate_national._admin_properties_sha256(values)
            for layer, values in rows.items()}
        metadata = self._metadata()
        metadata['description'] = validate_national._canonical_json_bytes({
            'schema': 'nwmm-national-admin-pmtiles-v1',
            'vintage': 'January 1 2025',
            'counts': counts,
            'fips_ids_sha256': id_hashes,
            'inventory_sha256':
                validate_national.ADMIN_SOURCE_INVENTORY_SHA256,
        }).decode('utf-8')
        metadata_hash = hashlib.sha256(
            validate_national._canonical_json_bytes(metadata)).hexdigest()
        options_hash = hashlib.sha256(validate_national._canonical_json_bytes(
            metadata['generator_options'])).hexdigest()
        clips = {
            'schema_version': 1,
            'source': (validate_national.ADMIN_SOURCE +
                       '/0 (States) and /1 (Counties), January 1 2025 vintage'),
            'note': 'Build-side/Lambda spatial clips only.',
            'states': {code: _square(index)
                       for index, code in enumerate(
                           validate_national.ALL_STATES)},
        }
        with mock.patch.dict(validate_national.ADMIN_COUNTS, counts, clear=True), \
                mock.patch.dict(validate_national.ADMIN_FIPS_IDS_SHA256,
                                id_hashes, clear=True), \
                mock.patch.dict(validate_national.ADMIN_PROPERTIES_SHA256,
                                property_hashes, clear=True), \
                mock.patch.dict(validate_national.ADMIN_MAXZOOM_INSTANCES,
                                counts, clear=True), \
                mock.patch.object(validate_national, 'ADMIN_METADATA_SHA256',
                                  metadata_hash), \
                mock.patch.object(validate_national,
                                  'ADMIN_GENERATOR_OPTIONS_SHA256', options_hash), \
                mock.patch.object(validate_national.os.path, 'isfile',
                                  return_value=True), \
                mock.patch.object(validate_national.os.path, 'getsize',
                                  side_effect=[
                                      validate_national.ADMIN_ARTIFACT_BYTES,
                                      validate_national.ADMIN_STATE_CLIPS_BYTES]), \
                mock.patch.object(validate_national, '_sha256_file', side_effect=[
                    validate_national.ADMIN_ARTIFACT_SHA256,
                    validate_national.ADMIN_STATE_CLIPS_SHA256]), \
                mock.patch.object(validate_national, '_load_json',
                                  return_value=clips), \
                mock.patch.object(validate_national, '_admin_pmtiles_metadata',
                                  return_value=metadata):
            manifest = {
                'sources': {
                    'boundaries': validate_national.ADMIN_BOUNDARIES_SOURCE},
                'national_baselines': {
                    'admin': validate_national._admin_expected_descriptor()},
            }
            qa = validate_national.QA()
            validate_national.validate_admin_baseline(
                qa, manifest, pmtiles_header=self._valid_scan)
        self.assertEqual(qa.errors, [])

    def test_wrong_clip_source_and_repro_metadata_fail_closed(self):
        self.assertTrue(any('state clips bytes/SHA-256' in error
                            for error in self._run(
                                self._manifest(), clip_sha='0' * 64).errors))
        wrong_source = self._manifest()
        wrong_source['sources']['boundaries'] = 'self-consistent forgery'
        self.assertTrue(any('sources.boundaries' in error
                            for error in self._run(wrong_source).errors))
        wrong_snapshot = self._manifest()
        wrong_snapshot['national_baselines']['admin']['source_snapshot'][
            'layers']['states']['object_ids_sha256'] = '0' * 64
        self.assertTrue(any('descriptor is missing' in error
                            for error in self._run(wrong_snapshot).errors))
        wrong_repro = self._manifest()
        wrong_repro['national_baselines']['admin']['reproducible_metadata'][
            'metadata_sha256'] = '0' * 64
        self.assertTrue(any('descriptor is missing' in error
                            for error in self._run(wrong_repro).errors))
        metadata = self._metadata()
        metadata['generator_options'] = '/private/root/input.geojsonseq'
        self.assertTrue(any('path-free reproducible' in error
                            for error in self._run(
                                self._manifest(), metadata=metadata).errors))

    def test_truncated_or_semantically_forged_archive_cannot_pass(self):
        def truncated(*args, **kwargs):
            raise ValueError('tile payload is truncated')
        self.assertTrue(any('truncated' in error
                            for error in self._run(
                                self._manifest(), header=truncated).errors))

        def missing_properties(path, layers, properties, **kwargs):
            kwargs['feature_validator']('states', {
                'id': 1, 'properties': {'fips': '01', 'st': 'AL'},
                'geometry_type': 3}, True)
        self.assertTrue(any('properties must be exactly' in error
                            for error in self._run(
                                self._manifest(), header=missing_properties).errors))

        def wrong_top_level_id(path, layers, properties, **kwargs):
            kwargs['feature_validator']('states', {
                'id': 2,
                'properties': {
                    'fips': '01', 'name': 'Alabama', 'st': 'AL'},
                'geometry_type': 3}, True)
        self.assertTrue(any('FIPS/name/state is invalid' in error
                            for error in self._run(
                                self._manifest(),
                                header=wrong_top_level_id).errors))

    def test_public_tree_requires_exact_descriptor_for_admin_exemption(self):
        valid = self._manifest()
        self.assertIn('data/tiles/context/admin.pmtiles',
                      validate_national._expected_public_pmtiles(valid))
        valid['national_baselines']['admin']['bytes'] -= 1
        self.assertNotIn('data/tiles/context/admin.pmtiles',
                         validate_national._expected_public_pmtiles(valid))

    def test_builder_validator_import_order_is_acyclic(self):
        for order in (
                'import validate_national, build_admin_pmtiles',
                'import build_admin_pmtiles, validate_national'):
            result = subprocess.run(
                [sys.executable, '-c',
                 "import sys; sys.path.insert(0, 'pipelines'); " + order],
                cwd=ROOT, capture_output=True, text=True)
            with self.subTest(order=order):
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_all_advertised_state_survey_clips_bind_reviewed_generation(self):
        with open(os.path.join(ROOT, 'site', 'data', 'manifest.json'),
                  encoding='utf-8') as source:
            manifest = json.load(source)
        baselines = manifest['national_baselines']
        recognized = set().union(*(
            group['keys']
            for group in validate_national._STATE_SURVEY_BASELINE_GROUPS.values()))

        def clips(value):
            if isinstance(value, dict):
                for key, nested in value.items():
                    if key == 'spatial_clip':
                        yield nested
                    else:
                        yield from clips(nested)
            elif isinstance(value, list):
                for nested in value:
                    yield from clips(nested)

        advertised = [
            clip for baseline_id in sorted(recognized & set(baselines))
            for clip in clips(baselines[baseline_id])]
        self.assertGreater(len(advertised), 0)
        self.assertTrue(all(
            clip.get('artifact') == 'infra/state_clips.json' and
            clip.get('artifact_sha256') ==
            validate_national.ADMIN_STATE_CLIPS_SHA256
            for clip in advertised))
        clip_path = os.path.join(ROOT, 'infra', 'state_clips.json')
        with open(clip_path, 'rb') as source:
            self.assertEqual(hashlib.sha256(source.read()).hexdigest(),
                             validate_national.ADMIN_STATE_CLIPS_SHA256)
        self.assertEqual(
            validate_national._admin_expected_descriptor()['state_clips'], {
                'file': 'infra/state_clips.json', 'bytes': 707_923,
                'sha256': validate_national.ADMIN_STATE_CLIPS_SHA256})
        qa = validate_national.QA()
        validate_national._validate_admin_clip_bindings(qa, baselines)
        self.assertEqual(qa.errors, [])

        forged = json.loads(json.dumps(baselines))
        first_key = next(key for key in sorted(recognized & set(forged))
                         if list(clips(forged[key])))
        next(clips(forged[first_key]))['artifact_sha256'] = '0' * 64
        qa = validate_national.QA()
        validate_national._validate_admin_clip_bindings(qa, forged)
        self.assertTrue(any(first_key in error for error in qa.errors))


if __name__ == '__main__':
    unittest.main()
