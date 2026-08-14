import importlib.util
import io
import json
import pathlib
import tempfile
import unittest


PROJECT = pathlib.Path(__file__).resolve().parents[1]
REPO = PROJECT.parent


def load_ci_module(name):
    path = PROJECT / 'ci' / f'{name}.py'
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class LfsDeliveryTests(unittest.TestCase):
    def test_tiled_binary_formats_are_lfs_objects(self):
        attributes = (REPO / '.gitattributes').read_text(encoding='utf-8')
        for suffix in ('*.pmtiles', '*.tif', '*.tiff'):
            self.assertIn(
                f'{suffix} filter=lfs diff=lfs merge=lfs -text', attributes)

    def test_both_ci_jobs_fetch_and_verify_lfs_payloads(self):
        workflow = (REPO / '.github' / 'workflows' / 'ws11.yml').read_text(
            encoding='utf-8')
        self.assertEqual(workflow.count('runs-on: ubuntu-24.04'), 2)
        self.assertEqual(workflow.count('timeout-minutes: 90'), 2)
        self.assertIn('cancel-in-progress: true', workflow)
        self.assertIn('contents: read', workflow)
        self.assertNotIn('ubuntu-latest', workflow)
        self.assertEqual(workflow.count(
            'uses: actions/checkout@11d5960a326750d5838078e36cf38b85af677262'), 2)
        self.assertEqual(workflow.count(
            'uses: actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065'), 2)
        self.assertEqual(workflow.count(
            'uses: actions/setup-node@49933ea5288caeca8642d1e84afbd3f7d6820020'), 2)
        self.assertEqual(workflow.count('lfs: true'), 2)
        self.assertEqual(workflow.count('git lfs pull'), 2)
        self.assertEqual(
            workflow.count('git lfs fsck --objects --pointers'), 2)
        self.assertEqual(workflow.count('python3 ci/verify_lfs.py'), 2)
        self.assertNotRegex(workflow, r'uses: actions/[^@\s]+@v\d')

    def test_ci_forces_dependency_gated_tests_and_strict_skip_policy(self):
        workflow = (REPO / '.github' / 'workflows' / 'ws11.yml').read_text(
            encoding='utf-8')
        native_setup = (PROJECT / 'ci' / 'install_tippecanoe.sh').read_text(
            encoding='utf-8')
        self.assertIn('bash ci/install_tippecanoe.sh', workflow)
        self.assertEqual(workflow.count('gdal-bin'), 1)
        self.assertIn('gdal-bin', native_setup)
        self.assertIn('python3 ci/verify_runtime.py', workflow)
        self.assertIn('python3 ci/run_tests.py', workflow)
        self.assertIn('python3 pipelines/validate_doc_store.py', workflow)
        self.assertIn('--only-binary=:all:', workflow)
        self.assertNotIn('python3 -m unittest discover', workflow)
        requirements = (PROJECT / 'ci' / 'requirements.txt').read_text(
            encoding='utf-8')
        self.assertIn('Fiona==1.9.5', requirements)
        self.assertIn('numpy==1.26.4', requirements)
        self.assertIn('Shapely==2.0.3', requirements)

    def test_checked_out_baselines_are_not_lfs_pointer_text(self):
        for relative in (
                'site/data/tiles/national/geology.pmtiles',
                'site/data/tiles/national/faults.pmtiles'):
            path = PROJECT / relative
            self.assertTrue(path.is_file(), relative)
            with path.open('rb') as source:
                self.assertEqual(source.read(8), b'PMTiles\x03')

    def test_lfs_inventory_covers_manifest_and_direct_ui_assets(self):
        verify_lfs = load_ci_module('verify_lfs')
        expected = {
            path.relative_to(PROJECT).as_posix()
            for path in verify_lfs.expected_assets()
        }
        self.assertIn('site/data/tiles/national/geology.pmtiles', expected)
        self.assertIn('site/data/tiles/context/admin.pmtiles', expected)
        present = {
            path.relative_to(PROJECT).as_posix()
            for path in verify_lfs.tiled_files(PROJECT / 'site')
        }
        self.assertEqual(expected, present)

    def test_lfs_verifier_rejects_pointer_stubs_and_wrong_magic(self):
        verify_lfs = load_ci_module('verify_lfs')
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            pointer = root / 'pointer.pmtiles'
            pointer.write_bytes(
                b'version https://git-lfs.github.com/spec/v1\n'
                b'oid sha256:' + b'0' * 64 + b'\nsize 8\n')
            with self.assertRaisesRegex(RuntimeError, 'not materialized'):
                verify_lfs.validate_materialized_asset(pointer)
            wrong = root / 'wrong.tif'
            wrong.write_bytes(b'not-a-tiff')
            with self.assertRaisesRegex(RuntimeError, 'magic is absent'):
                verify_lfs.validate_materialized_asset(wrong)

    def test_lfs_manifest_paths_cannot_escape_site(self):
        verify_lfs = load_ci_module('verify_lfs')
        with tempfile.TemporaryDirectory() as directory:
            project = pathlib.Path(directory)
            site = project / 'site'
            (site / 'data').mkdir(parents=True)
            (site / 'data' / 'manifest.json').write_text(json.dumps({
                'bad': {'file': 'data/../../outside.pmtiles'},
            }), encoding='utf-8')
            (site / 'index.html').write_text('', encoding='utf-8')
            with self.assertRaisesRegex(RuntimeError, 'escapes'):
                verify_lfs.expected_assets(project)

    def test_strict_runner_allows_only_reviewed_source_cache_skips(self):
        run_tests = load_ci_module('run_tests')
        self.assertEqual(set(run_tests.ALLOWED_SKIPS), {
            ('test_alaska_grade_evidence.AlaskaGradeEvidenceTests.'
             'test_full_local_build_is_byte_reproducible'),
            ('test_arizona_grade_evidence.ArizonaGradeEvidenceTests.'
             'test_full_local_build_matches_national_contract_and_does_not_release'),
            ('test_colorado_grade_evidence.ColoradoGradeEvidenceTests.'
             'test_full_local_build_is_reproducible_and_does_not_publish'),
            ('test_nevada_grade_evidence.NevadaGradeEvidenceTests.'
             'test_full_local_build_matches_national_contract_and_does_not_release'),
            ('test_new_mexico_grade_evidence.NewMexicoGradeEvidenceTests.'
             'test_full_build_matches_national_contract_and_never_releases'),
            ('test_south_dakota_grade_evidence.SouthDakotaGradeEvidenceTests.'
             'test_full_local_build_is_reproducible_and_does_not_publish'),
            ('test_utah_grade_evidence.UtahGradeEvidenceTests.'
             'test_two_full_local_builds_are_byte_identical_and_do_not_publish'),
        })
        self.assertFalse(any('Shapely' in reason or 'tippecanoe' in reason
                             for reason in run_tests.ALLOWED_SKIPS.values()))

        class FakeTest:
            def __init__(self, test_id):
                self.test_id = test_id

            def id(self):
                return self.test_id

            def __str__(self):
                return self.test_id

        result = run_tests.StrictSkipResult(io.StringIO(), True, 0)
        allowed_id, allowed_reason = next(iter(
            run_tests.ALLOWED_SKIPS.items()))
        result.addSkip(FakeTest(allowed_id), allowed_reason)
        self.assertEqual(result.unreviewed_skips, [])
        result.addSkip(FakeTest(allowed_id), 'reason drifted')
        result.addSkip(FakeTest('some.other.test'), allowed_reason)
        self.assertEqual(result.unreviewed_skips, [
            (allowed_id, 'reason drifted'),
            ('some.other.test', allowed_reason),
        ])
        self.assertFalse(result.wasSuccessful())


if __name__ == '__main__':
    unittest.main()
