import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'pipelines'))

import state_registry
import validate_public_site as public_site
import validate_release_assets as release_assets


def _state(path, sha256, size, *, enabled=True):
    return {
        'release': {
            'enabled': enabled,
            'status': 'done' if enabled else 'building',
        },
        'done_gate': {
            key: {'status': 'pass'} for key in state_registry.GATE_KEYS
        },
        'geology': {
            'artifact': path,
            'sha256': sha256,
            'bytes': size,
        },
    }


def _write_authorized(site, payload=b'{"release":true}\n', state='NV'):
    digest = hashlib.sha256(payload).hexdigest()
    relative = f'map-assets/releases/{state.lower()}/{digest}.json'
    target = Path(site, *relative.split('/'))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)
    return relative, digest, len(payload), target


class ReleaseAssetValidatorTests(unittest.TestCase):
    def test_collector_uses_exact_registry_sibling_matrix(self):
        def identity(name, suffix):
            digest = hashlib.sha256(name.encode()).hexdigest()
            path = f'map-assets/releases/fixture/{digest}.{suffix}'
            return path, digest, len(name) + 200

        def delivery(name, suffix='pmtiles'):
            path, digest, size = identity(name, suffix)
            return {'artifact': path, 'sha256': digest, 'bytes': size}

        geology = delivery('geology')
        faults = delivery('faults')
        zero_path, zero_sha, zero_bytes = identity('zero', 'json')
        faults['zero_inventory'] = {
            'evidence_artifact': zero_path, 'sha256': zero_sha,
            'bytes': zero_bytes}
        aeromag = delivery('aeromag', 'tif')
        claims = delivery('claims')
        open_ground = delivery('open-ground')
        alaska_claims = delivery('alaska-claims')
        publication_path, publication_sha, publication_bytes = identity(
            'publication', 'json')
        alaska_publication_path, alaska_publication_sha, alaska_publication_bytes = (
            identity('alaska-publication', 'json'))
        grade_path, grade_sha, grade_bytes = identity('grade', 'json')
        recorder_path, recorder_sha, recorder_bytes = identity('recorder', 'json')
        watch_path, watch_sha, watch_bytes = identity('watch', 'json')
        ranked_path, ranked_sha, ranked_bytes = identity('ranked', 'json')
        inventory_path, inventory_sha, inventory_bytes = identity('inventory', 'json')
        ci_path, ci_sha, ci_bytes = identity('ci', 'json')
        row = {
            'regime': 'claim',
            'release': {
                'enabled': True, 'status': 'done',
                'acceptance': {
                    'grades': {
                        'evidence_artifact': grade_path, 'sha256': grade_sha,
                        'bytes': grade_bytes},
                    # PP610 reuses the grade release file; source_sha256 is an
                    # upstream document hash and is intentionally not upload metadata.
                    'district_anchor': {
                        'artifact': grade_path, 'source_sha256': 'f' * 64},
                    'recorders': {
                        'evidence_artifact': recorder_path,
                        'evidence_sha256': recorder_sha,
                        'evidence_bytes': recorder_bytes},
                    'expiration_watch': {
                        'evidence_artifact': watch_path,
                        'evidence_sha256': watch_sha,
                        'evidence_bytes': watch_bytes},
                    'quad_maps': {
                        'ranked_targets_artifact': ranked_path,
                        'ranked_targets_sha256': ranked_sha,
                        'ranked_targets_bytes': ranked_bytes,
                        'targets': [{
                            'inventory_artifact': inventory_path,
                            'inventory_sha256': inventory_sha,
                            'inventory_bytes': inventory_bytes}]},
                    'ci_scale': {
                        'evidence_artifact': ci_path, 'sha256': ci_sha,
                        'bytes': ci_bytes},
                },
            },
            'done_gate': {
                key: {'status': 'pass'} for key in state_registry.GATE_KEYS},
            'geology': geology, 'faults': faults, 'aeromag': aeromag,
            'claim_systems': [{
                'id': 'federal_mlrs',
                'publication_artifacts': {
                    'claims': claims, 'open_ground': open_ground},
                'publication_inventory_artifact': publication_path,
                'publication_inventory_sha256': publication_sha,
                'publication_inventory_bytes': publication_bytes,
            }, {
                'id': 'alaska_state_claims',
                **alaska_claims,
                # Only federal_mlrs has split publication artifacts. Unknown
                # non-federal nested data cannot replace or expand its root delivery.
                'publication_artifacts': {'ignored': delivery('not-authorized')},
                'publication_inventory_artifact': alaska_publication_path,
                'publication_inventory_sha256': alaska_publication_sha,
                'publication_inventory_bytes': alaska_publication_bytes,
            }],
            # Unknown registry metadata cannot expand the upload scope.
            'research': {'artifact': 'map-assets/releases/not-authorized.json'},
        }
        allowlist = release_assets.derive_allowlist({'NV': row})
        expected = {
            item[0] for item in (
                (geology['artifact'],), (faults['artifact'],), (zero_path,),
                (aeromag['artifact'],), (claims['artifact'],),
                (open_ground['artifact'],), (alaska_claims['artifact'],),
                (publication_path,), (alaska_publication_path,), (grade_path,),
                (recorder_path,), (watch_path,), (ranked_path,),
                (inventory_path,), (ci_path,))}
        self.assertEqual(set(allowlist), expected)
        self.assertNotIn('map-assets/releases/not-authorized.json', allowlist)

    def test_exact_referenced_artifact_passes(self):
        with tempfile.TemporaryDirectory() as root:
            site = Path(root, 'site')
            relative, digest, size, _ = _write_authorized(site)
            allowlist = release_assets.validate_release_assets(
                site, {'NV': _state(relative, digest, size)})
            self.assertEqual(list(allowlist), [relative])
            self.assertEqual(allowlist[relative].bytes, size)
            self.assertEqual(allowlist[relative].sha256, digest)

    def test_zero_released_states_requires_zero_files(self):
        with tempfile.TemporaryDirectory() as root:
            site = Path(root, 'site')
            self.assertEqual(
                release_assets.validate_release_assets(site, {}), {})
            (site / 'map-assets' / 'releases').mkdir(parents=True)
            self.assertEqual(
                release_assets.validate_release_assets(site, {}), {})

    def test_orphan_and_disabled_state_artifacts_are_rejected(self):
        for disabled in (False, True):
            with self.subTest(disabled=disabled), tempfile.TemporaryDirectory() as root:
                site = Path(root, 'site')
                relative, digest, size, folder_file = _write_authorized(site)
                states = ({'NV': _state(relative, digest, size)} if not disabled else
                          {'NV': _state(relative, digest, size, enabled=False)})
                if not disabled:
                    orphan_payload = b'orphan\n'
                    orphan_sha = hashlib.sha256(orphan_payload).hexdigest()
                    orphan = folder_file.parent / f'{orphan_sha}.json'
                    orphan.write_bytes(orphan_payload)
                with self.assertRaisesRegex(
                        release_assets.ReleaseAssetError, 'unexpected release files'):
                    release_assets.validate_release_assets(site, states)

    def test_hidden_temporary_and_symlink_paths_are_rejected(self):
        cases = ('hidden', 'temporary', 'symlink')
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as root:
                site = Path(root, 'site')
                release_root = site / 'map-assets' / 'releases'
                release_root.mkdir(parents=True)
                if case == 'hidden':
                    (release_root / '.DS_Store').write_bytes(b'x')
                elif case == 'temporary':
                    (release_root / 'pending.tmp').write_bytes(b'x')
                else:
                    source = Path(root, 'outside.json')
                    source.write_bytes(b'x')
                    (release_root / 'linked.json').symlink_to(source)
                with self.assertRaises(release_assets.ReleaseAssetError) as raised:
                    release_assets.validate_release_assets(site, {})
                self.assertRegex(str(raised.exception),
                                 'hidden/temporary|symlinks are forbidden')

    def test_wrong_bytes_and_wrong_hash_are_rejected(self):
        payload = b'exact immutable bytes\n'
        digest = hashlib.sha256(payload).hexdigest()
        wrong_digest = hashlib.sha256(b'different').hexdigest()
        cases = (
            ('bytes', digest, len(payload) + 1, 'bytes'),
            ('hash', wrong_digest, len(payload), 'SHA-256'),
        )
        for name, registry_sha, registry_size, expected in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as root:
                site = Path(root, 'site')
                relative = f'map-assets/releases/nv/{registry_sha}.json'
                target = site.joinpath(*relative.split('/'))
                target.parent.mkdir(parents=True)
                target.write_bytes(payload)
                with self.assertRaisesRegex(release_assets.ReleaseAssetError, expected):
                    release_assets.validate_release_assets(
                        site, {'NV': _state(relative, registry_sha, registry_size)})

    def test_requires_bytes_sha_and_content_addressed_basename(self):
        digest = hashlib.sha256(b'x').hexdigest()
        relative = f'map-assets/releases/nv/{digest}.json'
        row = _state(relative, digest, 1)
        del row['geology']['bytes']
        with self.assertRaisesRegex(release_assets.ReleaseAssetError,
                                    'registry byte count is missing'):
            release_assets.derive_allowlist({'NV': row})
        row = _state('map-assets/releases/nv/not-a-digest.json', digest, 1)
        with self.assertRaisesRegex(release_assets.ReleaseAssetError,
                                    'basename must be'):
            release_assets.derive_allowlist({'NV': row})

    def test_enabled_state_with_failed_gate_cannot_authorize_upload(self):
        digest = hashlib.sha256(b'x').hexdigest()
        relative = f'map-assets/releases/nv/{digest}.json'
        row = _state(relative, digest, 1)
        row['done_gate']['grades']['status'] = 'fail'
        with self.assertRaisesRegex(release_assets.ReleaseAssetError,
                                    'incomplete DONE gates: grades'):
            release_assets.derive_allowlist({'NV': row})


class PublicSiteValidatorTests(unittest.TestCase):
    @staticmethod
    def _empty_site(root):
        site = Path(root, 'site')
        (site / 'data').mkdir(parents=True)
        (site / 'data' / 'manifest.json').write_text('{}\n', encoding='utf-8')
        (site / 'index.html').write_text('<!doctype html>\n', encoding='utf-8')
        return site

    @staticmethod
    def _write_binary(site, relative, payload):
        target = site.joinpath(*relative.split('/'))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        return {
            'format': 'pmtiles' if relative.endswith('.pmtiles') else 'cog',
            'file': relative,
            'bytes': len(payload),
            'sha256': hashlib.sha256(payload).hexdigest(),
        }

    def test_exact_allowlist_is_derived_only_from_manifest(self):
        with tempfile.TemporaryDirectory() as root:
            site = self._empty_site(root)
            manifest_relative = 'data/tiles/national/manifest.pmtiles'
            ui_relative = 'data/tiles/context/ui.pmtiles'
            cog_relative = 'data/tiles/national/surface.tif'
            manifest_descriptor = self._write_binary(
                site, manifest_relative, b'manifest archive')
            self._write_binary(site, ui_relative, b'ui archive')
            cog_descriptor = self._write_binary(site, cog_relative, b'cog archive')
            cog_descriptor['url'] = cog_descriptor.pop('file')
            manifest = {
                'national_baselines': {
                    'fixture': manifest_descriptor,
                    'ui_resolved_fixture': self._write_binary(
                        site, ui_relative, b'ui archive'),
                },
                'tiled_layers': [{'delivery': {'cog': cog_descriptor}}],
            }
            (site / 'data' / 'manifest.json').write_text(
                json.dumps(manifest), encoding='utf-8')
            (site / 'index.html').write_text('<script>loadManifest();</script>\n',
                                             encoding='utf-8')

            allowlist = public_site.validate_public_site(site)

            self.assertEqual(list(allowlist), sorted((
                manifest_relative, ui_relative, cog_relative)))
            for relative, identity in allowlist.items():
                target = site.joinpath(*relative.split('/'))
                self.assertEqual(identity.bytes, target.stat().st_size)
                self.assertEqual(identity.sha256,
                                 hashlib.sha256(target.read_bytes()).hexdigest())

    def test_literal_ui_binary_path_is_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            site = self._empty_site(root)
            relative = 'data/tiles/context/ui.pmtiles'
            descriptor = self._write_binary(site, relative, b'ui archive')
            (site / 'data' / 'manifest.json').write_text(
                json.dumps({'national_baselines': {'ui': descriptor}}),
                encoding='utf-8')
            (site / 'index.html').write_text(
                f'<script>const forbidden = "pmtiles://{relative}";</script>\n',
                encoding='utf-8')
            with self.assertRaisesRegex(
                    public_site.PublicSiteError, 'must resolve through the deployment manifest'):
                public_site.validate_public_site(site)

    def test_orphan_public_binaries_are_rejected_everywhere(self):
        for relative in ('data/tiles/orphan.pmtiles', 'assets/orphan.tif'):
            with self.subTest(relative=relative), tempfile.TemporaryDirectory() as root:
                site = self._empty_site(root)
                target = site.joinpath(*relative.split('/'))
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b'orphan')
                with self.assertRaisesRegex(
                        public_site.PublicSiteError, 'unexpected public binaries'):
                    public_site.validate_public_site(site)

    def test_hidden_temp_backup_symlink_and_sensitive_paths_are_rejected(self):
        cases = {
            'hidden': ('data/.private.json', 'file'),
            'temporary': ('assets/bundle.js.tmp', 'file'),
            'backup': ('data/backup/stale.json', 'file'),
            'symlink': ('assets/linked.js', 'symlink'),
            'private-key': ('assets/server.pem', 'file'),
            'credentials': ('data/config/credentials.json', 'file'),
        }
        for name, (relative, kind) in cases.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as root:
                site = self._empty_site(root)
                target = site.joinpath(*relative.split('/'))
                target.parent.mkdir(parents=True, exist_ok=True)
                if kind == 'symlink':
                    outside = Path(root, 'outside.js')
                    outside.write_text('secret\n', encoding='utf-8')
                    target.symlink_to(outside)
                else:
                    target.write_text('secret\n', encoding='utf-8')
                with self.assertRaises(public_site.PublicSiteError) as raised:
                    public_site.validate_public_site(site)
                self.assertRegex(
                    str(raised.exception),
                    'hidden|temporary/backup|symlinks are forbidden|sensitive')

    def test_manifest_identity_mismatch_is_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            site = self._empty_site(root)
            relative = 'data/tiles/national/fixture.pmtiles'
            descriptor = self._write_binary(site, relative, b'local bytes')
            descriptor['sha256'] = hashlib.sha256(b'other bytes').hexdigest()
            (site / 'data' / 'manifest.json').write_text(
                json.dumps({'national_baselines': {'fixture': descriptor}}),
                encoding='utf-8')
            with self.assertRaisesRegex(
                    public_site.PublicSiteError, 'SHA-256 mismatch'):
                public_site.validate_public_site(site)

    def test_deployment_bundle_rewrites_exact_and_protocol_paths(self):
        with tempfile.TemporaryDirectory() as root:
            site = self._empty_site(root)
            relative = 'data/tiles/states/co/fixture.pmtiles'
            descriptor = self._write_binary(site, relative, b'fixture archive')
            descriptor.update({
                'bounds': [-109, 37, -102, 41],
                'minzoom': 0,
                'maxzoom': 12,
                'browser_descriptor': {
                    'file': relative,
                    'protocol_url': f'pmtiles://{relative}',
                    'bounds': [-109, 37, -102, 41],
                    'minzoom': 0,
                    'maxzoom': 12,
                },
            })
            manifest = {'national_baselines': {'fixture': descriptor}}
            (site / 'data' / 'manifest.json').write_text(
                json.dumps(manifest), encoding='utf-8')

            bundle = public_site.build_deployment_bundle(site)

            self.assertEqual(len(bundle.binaries), 1)
            item = bundle.binaries[0]
            expected_key = (
                f'map-assets/baselines/{hashlib.sha256(b"fixture archive").hexdigest()}'
                '.pmtiles')
            self.assertEqual(item.local_path, relative)
            self.assertEqual(item.object_path, expected_key)
            deployed = bundle.manifest['national_baselines']['fixture']
            self.assertEqual(deployed['file'], expected_key)
            self.assertEqual(deployed['browser_descriptor']['file'], expected_key)
            self.assertEqual(
                deployed['browser_descriptor']['protocol_url'],
                f'pmtiles://{expected_key}')
            self.assertEqual(deployed['bytes'], descriptor['bytes'])
            self.assertEqual(deployed['sha256'], descriptor['sha256'])
            self.assertNotIn(relative, json.dumps(bundle.manifest))

    def test_identical_digest_is_reused_by_deployment_bundle(self):
        with tempfile.TemporaryDirectory() as root:
            site = self._empty_site(root)
            payload = b'identical archive'
            first = 'data/tiles/national/first.pmtiles'
            second = 'data/tiles/states/co/second.pmtiles'
            manifest = {'national_baselines': {
                'first': self._write_binary(site, first, payload),
                'second': self._write_binary(site, second, payload),
            }}
            (site / 'data' / 'manifest.json').write_text(
                json.dumps(manifest), encoding='utf-8')

            bundle = public_site.build_deployment_bundle(site)

            self.assertEqual(len(bundle.binaries), 1)
            key = bundle.binaries[0].object_path
            self.assertEqual(bundle.manifest['national_baselines']['first']['file'], key)
            self.assertEqual(bundle.manifest['national_baselines']['second']['file'], key)

    def test_rewrite_rejects_substring_query_escape_and_conflicting_identity(self):
        digest = hashlib.sha256(b'fixture').hexdigest()
        relative = 'data/tiles/national/fixture.pmtiles'
        mapping = {relative: f'map-assets/baselines/{digest}.pmtiles'}
        self.assertIn(relative, public_site._manifest_binary_references({
            'protocol_url': f'pmtiles://{relative}',
        }))
        for name, value in {
            'query': f'{relative}?v=2',
            'protocol-query': f'pmtiles://{relative}?v=2',
            'substring': f'prefix-{relative}',
        }.items():
            with self.subTest(name=name), self.assertRaisesRegex(
                    public_site.PublicSiteError, 'query strings and substrings'):
                public_site._rewrite_deployment_manifest({'value': value}, mapping)

        with self.assertRaisesRegex(
                public_site.PublicSiteError, 'query strings and substrings'):
            public_site._manifest_binary_references({
                'protocol_url': f'pmtiles://{relative}?v=2',
            })
        with self.assertRaisesRegex(
                public_site.PublicSiteError, 'generated only'):
            public_site._manifest_binary_references({
                'file': f'map-assets/baselines/{digest}.pmtiles',
            })

        with self.assertRaisesRegex(
                public_site.PublicSiteError, 'non-canonical public binary path'):
            public_site._canonical_data_binary(
                'data/tiles/../secret.pmtiles', source='test')

        conflict = {
            'a': {'file': relative, 'bytes': 7, 'sha256': digest},
            'b': {'file': relative, 'bytes': 8, 'sha256': 'f' * 64},
        }
        with self.assertRaisesRegex(
                public_site.PublicSiteError, 'conflicting identity metadata'):
            public_site._manifest_binary_references(conflict)

    def test_browser_descriptor_mismatch_is_rejected_before_transform(self):
        with tempfile.TemporaryDirectory() as root:
            site = self._empty_site(root)
            relative = 'data/tiles/states/ut/fixture.pmtiles'
            descriptor = self._write_binary(site, relative, b'fixture archive')
            descriptor['browser_descriptor'] = {
                'file': relative,
                'protocol_url': 'pmtiles://wrong.example/fixture.pmtiles',
            }
            (site / 'data' / 'manifest.json').write_text(json.dumps({
                'national_baselines': {'fixture': descriptor},
            }), encoding='utf-8')
            with self.assertRaisesRegex(
                    public_site.PublicSiteError, 'protocol_url must equal'):
                public_site.build_deployment_bundle(site)


class ReleaseAssetDeployTests(unittest.TestCase):
    def _fixture(self, root, allowlist, validator_status=0, *,
                 public_allowlist=None, public_validator_status=0):
        root = Path(root)
        infra = root / 'infra'
        site = root / 'site'
        binary = root / 'bin'
        pipelines = root / 'pipelines'
        infra.mkdir()
        (site / 'data').mkdir(parents=True)
        (site / 'index.html').write_text('<!doctype html>\n', encoding='utf-8')
        (site / 'data' / 'coverage.json').write_text('{}\n', encoding='utf-8')
        (site / 'data' / 'manifest.json').write_text('{}\n', encoding='utf-8')
        baseline_payload = b'verified public baseline\n'
        baseline_dir = site / 'data' / 'tiles' / 'fixture'
        baseline_dir.mkdir(parents=True)
        (baseline_dir / 'baseline.pmtiles').write_bytes(baseline_payload)
        (baseline_dir / 'baseline.tif').write_bytes(baseline_payload)
        if public_allowlist is None:
            public_allowlist = (
                'data/tiles/fixture/baseline.pmtiles',
                'data/tiles/fixture/baseline.tif',
            )
        fixture_manifest = {'national_baselines': {}}
        for index, relative in enumerate(public_allowlist):
            target = site.joinpath(*relative.split('/'))
            payload = target.read_bytes()
            fixture_manifest['national_baselines'][f'fixture_{index}'] = {
                'file': relative,
                'bytes': len(payload),
                'sha256': hashlib.sha256(payload).hexdigest(),
            }
        (site / 'data' / 'manifest.json').write_text(
            json.dumps(fixture_manifest), encoding='utf-8')
        binary.mkdir()
        pipelines.mkdir()
        shutil.copy2(ROOT / 'infra' / 'deploy.sh', infra / 'deploy.sh')
        validator = pipelines / 'validate_release_assets.py'
        validator.write_text(
            'import os, pathlib, sys\n'
            'status = int(os.environ.get("VALIDATOR_STATUS", "0"))\n'
            'if status:\n'
            '    print("validator rejected tree", file=sys.stderr)\n'
            '    raise SystemExit(status)\n'
            'sys.stdout.write(pathlib.Path(os.environ["VALIDATOR_ALLOWLIST"]).read_text())\n',
            encoding='utf-8')
        public_validator = pipelines / 'validate_public_site.py'
        public_validator.write_text(
            'import hashlib, json, os, pathlib, sys\n'
            'status = int(os.environ.get("PUBLIC_VALIDATOR_STATUS", "0"))\n'
            'if status:\n'
            '    print("public validator rejected tree", file=sys.stderr)\n'
            '    raise SystemExit(status)\n'
            'allowlist = [line for line in pathlib.Path(os.environ["PUBLIC_ALLOWLIST"]).read_text().splitlines() if line]\n'
            'if "--format" in sys.argv and sys.argv[sys.argv.index("--format") + 1] == "deployment":\n'
            '    manifest_output = pathlib.Path(sys.argv[sys.argv.index("--manifest-output") + 1])\n'
            '    mappings = {}\n'
            '    for relative in allowlist:\n'
            '        target = pathlib.Path(os.environ["FIXTURE_SITE"], *relative.split("/"))\n'
            '        payload = target.read_bytes()\n'
            '        digest = hashlib.sha256(payload).hexdigest()\n'
            '        suffix = pathlib.PurePosixPath(relative).suffix\n'
            '        key = f"map-assets/baselines/{digest}{suffix}"\n'
            '        mappings[relative] = key\n'
            '        b64 = __import__("base64").b64encode(bytes.fromhex(digest)).decode()\n'
            '        print("\\t".join((relative, key, str(len(payload)), digest, b64)))\n'
            '    source = pathlib.Path(os.environ["FIXTURE_SITE"], "data", "manifest.json")\n'
            '    manifest = json.loads(source.read_text())\n'
            '    def rewrite(value):\n'
            '        if isinstance(value, dict): return {k: rewrite(v) for k, v in value.items()}\n'
            '        if isinstance(value, list): return [rewrite(v) for v in value]\n'
            '        if isinstance(value, str) and value in mappings: return mappings[value]\n'
            '        if isinstance(value, str) and value.startswith("pmtiles://") and value[10:] in mappings: return "pmtiles://" + mappings[value[10:]]\n'
            '        return value\n'
            '    manifest_output.write_text(json.dumps(rewrite(manifest)) + "\\n")\n'
            'else:\n'
            '    sys.stdout.write("".join(item + "\\n" for item in allowlist))\n',
            encoding='utf-8')
        python = binary / 'python3'
        python.write_text(
            '#!/usr/bin/env sh\n'
            'case "$1" in\n'
            '  */validate_release_assets.py) exec "$REAL_PYTHON" "$@" ;;\n'
            '  */validate_public_site.py) exec "$REAL_PYTHON" "$@" ;;\n'
            '  -) exec "$REAL_PYTHON" "$@" ;;\n'
            'esac\n'
            'if [ "$1" = "-m" ]; then\n'
            '  exit 0\n'
            'fi\n'
            'exit 0\n', encoding='utf-8')
        python.chmod(0o755)
        allowlist_path = root / 'allowlist'
        allowlist_path.write_text(''.join(f'{item}\n' for item in allowlist),
                                  encoding='utf-8')
        public_allowlist_path = root / 'public-allowlist'
        public_allowlist_path.write_text(
            ''.join(f'{item}\n' for item in public_allowlist), encoding='utf-8')
        aws = binary / 'aws'
        aws.write_text(
            '#!/usr/bin/env sh\n'
            'printf "%s\\n" "$*" >> "$AWS_LOG"\n'
            'if [ "$1" = "s3" ] && [ "$2" = "cp" ]; then\n'
            '  case "$4" in\n'
            '    */data/manifest.json)\n'
            '      capture_count=$(find "$MANIFEST_CAPTURE_DIR" -type f | wc -l | tr -d " ")\n'
            '      /bin/cp "$3" "$MANIFEST_CAPTURE_DIR/manifest-$capture_count.json" ;;\n'
            '  esac\n'
            'fi\n'
            'case "$*" in\n'
            '  *"--version"*) echo "aws-cli/2.0" ;;\n'
            '  *"s3api put-object"*)\n'
            '    if [ "${FAIL_BASELINE_PUT:-}" = "second" ] &&\n'
            '       [ "$(grep -c "s3api put-object" "$AWS_LOG")" = "2" ]; then\n'
            '      exit 17\n'
            '    fi ;;\n'
            '  *"s3api head-object"*)\n'
            '    key_digest=$(printf "%s" "$*" | sed -n "s/.*--key map-assets\\/baselines\\/\\([0-9a-f][0-9a-f]*\\)\\..*/\\1/p")\n'
            '    if [ -n "$key_digest" ]; then\n'
            '      key_sha=$(printf "%s" "$key_digest" | xxd -r -p | base64 | tr -d "\\n")\n'
            '    else\n'
            '      key_sha="$BASELINE_SHA256_B64"\n'
            '    fi\n'
            '    case "${MISMATCH_BASELINE_HEAD:-}" in\n'
            '      size) printf "999\\t%s\\n" "$BASELINE_SHA256_B64" ;;\n'
            '      hash) printf "%s\\tbad-checksum\\n" "$BASELINE_SIZE" ;;\n'
            '      *)\n'
            '        if [ "${HEAD_BEFORE_PUT_MISSING:-0}" = "1" ] &&\n'
            '           [ "$(( $(grep -c "s3api head-object" "$AWS_LOG") % 2 ))" = "1" ]; then\n'
            '          exit 44\n'
            '        fi\n'
            '        printf "%s\\t%s\\n" "$BASELINE_SIZE" "$key_sha" ;;\n'
            '    esac ;;\n'
            '  *"BucketName"*) echo "fixture-bucket" ;;\n'
            '  *"DistributionId"*) echo "fixture-dist" ;;\n'
            'esac\n', encoding='utf-8')
        aws.chmod(0o755)
        log = root / 'aws.log'
        manifest_capture = root / 'manifest-captures'
        manifest_capture.mkdir()
        env = os.environ.copy()
        env.update({
            'PATH': f'{binary}{os.pathsep}{env["PATH"]}',
            'AWS_LOG': str(log),
            'MANIFEST_CAPTURE_DIR': str(manifest_capture),
            'VALIDATOR_ALLOWLIST': str(allowlist_path),
            'VALIDATOR_STATUS': str(validator_status),
            'PUBLIC_ALLOWLIST': str(public_allowlist_path),
            'PUBLIC_VALIDATOR_STATUS': str(public_validator_status),
            'FIXTURE_SITE': str(site),
            'REAL_PYTHON': sys.executable,
            'BASELINE_SIZE': str(len(baseline_payload)),
            'BASELINE_SHA256_B64': base64.b64encode(
                hashlib.sha256(baseline_payload).digest()).decode('ascii'),
            'HEAD_BEFORE_PUT_MISSING': '1',
        })
        return infra / 'deploy.sh', site, log, env

    def test_standalone_validator_failure_occurs_before_any_aws_call(self):
        with tempfile.TemporaryDirectory() as root:
            deploy, _, log, env = self._fixture(root, [], validator_status=9)
            result = subprocess.run(
                ['bash', str(deploy), 'upload-release-assets'], env=env,
                text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('validator rejected tree', result.stderr)
            self.assertFalse(log.exists(), 'AWS was called before validation failed')

    def test_standalone_zero_allowlist_makes_no_aws_call(self):
        with tempfile.TemporaryDirectory() as root:
            deploy, _, log, env = self._fixture(root, [])
            result = subprocess.run(
                ['bash', str(deploy), 'upload-release-assets'], env=env,
                text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('0 allowlisted files', result.stdout)
            self.assertFalse(log.exists())

    def test_public_tree_validator_failure_occurs_before_any_aws_call(self):
        with tempfile.TemporaryDirectory() as root:
            deploy, _, log, env = self._fixture(
                root, [], public_validator_status=8)
            result = subprocess.run(
                ['bash', str(deploy), 'update-site'], env=env,
                text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('public validator rejected tree', result.stderr)
            self.assertFalse(log.exists(), 'AWS was called before public validation failed')

    @staticmethod
    def _pointer_or_index_calls(calls):
        return [call for call in calls if any(item in call for item in (
            '/site/data/coverage.json ', '/nwmm-public-manifest.',
            '/site/index.html '))]

    def test_release_and_verified_baselines_precede_pointers_and_index(self):
        relative = 'map-assets/releases/nv/' + 'a' * 64 + '.json'
        with tempfile.TemporaryDirectory() as root:
            deploy, site, log, env = self._fixture(root, [relative])
            target = site.joinpath(*relative.split('/'))
            target.parent.mkdir(parents=True)
            target.write_bytes(b'fixture')
            result = subprocess.run(
                ['bash', str(deploy), 'update-site'], env=env,
                text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.assertEqual(result.returncode, 0, result.stderr)
            calls = log.read_text(encoding='utf-8').splitlines()
            release_upload = next(index for index, call in enumerate(calls)
                                  if ('s3 cp ' in call and relative in call))
            binary_puts = [index for index, call in enumerate(calls)
                           if 's3api put-object ' in call]
            binary_heads = [index for index, call in enumerate(calls)
                            if 's3api head-object ' in call]
            coverage_upload = next(index for index, call in enumerate(calls)
                                   if '/site/data/coverage.json ' in call)
            manifest_upload = next(index for index, call in enumerate(calls)
                                   if ('/nwmm-public-manifest.' in call and
                                       's3://fixture-bucket/data/manifest.json' in call))
            index_upload = next(index for index, call in enumerate(calls)
                                if 's3 cp ' in call and '/site/index.html ' in call)
            data_sync = next(index for index, call in enumerate(calls)
                             if 's3 sync ' in call and '/site/data ' in call)
            self.assertEqual(len(binary_puts), 2)
            self.assertEqual(len(binary_heads), 4)
            self.assertLess(release_upload, min(binary_puts))
            self.assertLess(max(binary_puts + binary_heads), data_sync)
            self.assertLess(data_sync, coverage_upload)
            self.assertLess(coverage_upload, manifest_upload)
            self.assertLess(manifest_upload, index_upload)
            for index in binary_puts:
                self.assertIn('--checksum-sha256 ', calls[index])
                self.assertIn('--cache-control public, max-age=31536000, immutable',
                              calls[index])
                self.assertIn('--key map-assets/baselines/', calls[index])
                self.assertNotIn('--key data/tiles/', calls[index])
            for index in binary_heads:
                self.assertIn('--checksum-mode ENABLED', calls[index])
            self.assertIn('--exclude *.pmtiles', calls[data_sync])
            self.assertIn('--exclude *.tif', calls[data_sync])
            self.assertIn('--exclude manifest.json', calls[data_sync])
            self.assertIn('--exclude coverage.json', calls[data_sync])
            self.assertIn(
                f's3://fixture-bucket/map-assets/releases/nv/{"a" * 64}.json',
                calls[release_upload])
            self.assertNotIn('/site/data/manifest.json ', calls[manifest_upload])
            self.assertFalse(any(
                's3 sync' in call and 'map-assets/releases' in call for call in calls))

    def test_binary_uploader_uses_only_validator_allowlisted_paths(self):
        allowed = 'data/tiles/fixture/baseline.pmtiles'
        with tempfile.TemporaryDirectory() as root:
            deploy, _, log, env = self._fixture(
                root, [], public_allowlist=[allowed])
            result = subprocess.run(
                ['bash', str(deploy), 'update-site'], env=env,
                text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.assertEqual(result.returncode, 0, result.stderr)
            calls = log.read_text(encoding='utf-8').splitlines()
            binary_puts = [call for call in calls if 's3api put-object ' in call]
            binary_heads = [call for call in calls if 's3api head-object ' in call]
            self.assertEqual(len(binary_puts), 1)
            self.assertEqual(len(binary_heads), 2)
            digest = hashlib.sha256(b'verified public baseline\n').hexdigest()
            self.assertIn(
                f'--key map-assets/baselines/{digest}.pmtiles ', binary_puts[0])
            self.assertNotIn(f'--key {allowed} ', binary_puts[0])
            self.assertFalse(any('baseline.tif' in call for call in calls))

    def test_two_generations_publish_distinct_content_keys_without_friendly_overwrite(self):
        allowed = 'data/tiles/fixture/baseline.pmtiles'
        with tempfile.TemporaryDirectory() as root:
            deploy, site, log, env = self._fixture(
                root, [], public_allowlist=[allowed])
            local = site.joinpath(*allowed.split('/'))

            first_payload = local.read_bytes()
            first_digest = hashlib.sha256(first_payload).hexdigest()
            first = subprocess.run(
                ['bash', str(deploy), 'update-site'], env=env,
                text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.assertEqual(first.returncode, 0, first.stderr)

            second_payload = b'next verified public baseline\n'
            local.write_bytes(second_payload)
            manifest_path = site / 'data' / 'manifest.json'
            manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
            descriptor = manifest['national_baselines']['fixture_0']
            descriptor['bytes'] = len(second_payload)
            descriptor['sha256'] = hashlib.sha256(second_payload).hexdigest()
            manifest_path.write_text(json.dumps(manifest), encoding='utf-8')
            env['BASELINE_SIZE'] = str(len(second_payload))
            env['BASELINE_SHA256_B64'] = base64.b64encode(
                hashlib.sha256(second_payload).digest()).decode('ascii')
            second_digest = hashlib.sha256(second_payload).hexdigest()
            second = subprocess.run(
                ['bash', str(deploy), 'update-site'], env=env,
                text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.assertEqual(second.returncode, 0, second.stderr)

            calls = log.read_text(encoding='utf-8').splitlines()
            puts = [call for call in calls if 's3api put-object ' in call]
            self.assertTrue(any(
                f'--key map-assets/baselines/{first_digest}.pmtiles ' in call
                for call in puts))
            self.assertTrue(any(
                f'--key map-assets/baselines/{second_digest}.pmtiles ' in call
                for call in puts))
            self.assertFalse(any(f'--key {allowed} ' in call for call in puts))
            self.assertFalse(any(
                's3 rm ' in call and 'map-assets/baselines' in call
                for call in calls))
            captures = Path(env['MANIFEST_CAPTURE_DIR'])
            first_manifest = json.loads(
                (captures / 'manifest-0.json').read_text(encoding='utf-8'))
            second_manifest = json.loads(
                (captures / 'manifest-1.json').read_text(encoding='utf-8'))
            self.assertEqual(
                first_manifest['national_baselines']['fixture_0']['file'],
                f'map-assets/baselines/{first_digest}.pmtiles')
            self.assertEqual(
                second_manifest['national_baselines']['fixture_0']['file'],
                f'map-assets/baselines/{second_digest}.pmtiles')
            self.assertNotEqual(first_manifest, second_manifest)

    def test_deploy_and_update_site_share_the_pointer_last_phase_order(self):
        script = (ROOT / 'infra' / 'deploy.sh').read_text(encoding='utf-8')
        phases = (
            'sync_public_site_without_pointers "$BUCKET"',
            'upload_and_verify_public_data_binaries "$BUCKET"',
            'sync_public_data_without_pointers_or_binaries "$BUCKET"',
            'upload_public_data_pointers "$BUCKET"',
            'upload_public_index "$BUCKET"',
        )
        locations = []
        for phase in phases:
            offsets = []
            start = 0
            while True:
                offset = script.find(phase, start)
                if offset < 0:
                    break
                offsets.append(offset)
                start = offset + len(phase)
            self.assertEqual(len(offsets), 2, phase)
            locations.append(offsets)
        for branch in range(2):
            ordered = [offsets[branch] for offsets in locations]
            self.assertEqual(ordered, sorted(ordered))

    def test_binary_upload_failure_does_not_advance_pointers_or_index(self):
        with tempfile.TemporaryDirectory() as root:
            deploy, _, log, env = self._fixture(root, [])
            env['FAIL_BASELINE_PUT'] = 'second'
            result = subprocess.run(
                ['bash', str(deploy), 'update-site'], env=env,
                text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.assertNotEqual(result.returncode, 0)
            calls = log.read_text(encoding='utf-8').splitlines()
            self.assertEqual(
                sum('s3api put-object ' in call for call in calls), 2)
            self.assertEqual(
                sum('s3api head-object ' in call for call in calls), 3)
            self.assertEqual(self._pointer_or_index_calls(calls), [])

    def test_binary_head_size_or_hash_mismatch_does_not_advance_pointers(self):
        for mismatch in ('size', 'hash'):
            with self.subTest(mismatch=mismatch), tempfile.TemporaryDirectory() as root:
                deploy, _, log, env = self._fixture(root, [])
                env['MISMATCH_BASELINE_HEAD'] = mismatch
                result = subprocess.run(
                    ['bash', str(deploy), 'update-site'], env=env,
                    text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(
                    'remote public baseline identity mismatch', result.stderr)
                calls = log.read_text(encoding='utf-8').splitlines()
                self.assertTrue(any('s3api put-object ' in call for call in calls))
                self.assertTrue(any('s3api head-object ' in call for call in calls))
                self.assertFalse(any(
                    's3 sync ' in call and '/site/data ' in call for call in calls))
                self.assertEqual(self._pointer_or_index_calls(calls), [])


if __name__ == '__main__':
    unittest.main()
