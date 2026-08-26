#!/usr/bin/env python3
"""Run unittest discovery and reject every unreviewed skip."""

from __future__ import annotations

import os
from pathlib import Path
import sys
import types
import unittest


PROJECT = Path(__file__).resolve().parents[1]

# Clean Git checkouts intentionally omit these checksum-pinned source PDFs.
# Their pure contract tests still run; only the full private rebuild is skipped.
ALLOWED_SKIPS = {
    ('test_alaska_grade_evidence.AlaskaGradeEvidenceTests.'
     'test_full_local_build_is_byte_reproducible'):
        'official checksum-pinned Alaska PDF cache is not installed',
    ('test_arizona_grade_evidence.ArizonaGradeEvidenceTests.'
     'test_full_local_build_matches_national_contract_and_does_not_release'):
        'official checksum-pinned PDF cache is not installed',
    ('test_colorado_grade_evidence.ColoradoGradeEvidenceTests.'
     'test_full_local_build_is_reproducible_and_does_not_publish'):
        'official checksum-pinned Colorado PDF cache is not installed',
    ('test_nevada_grade_evidence.NevadaGradeEvidenceTests.'
     'test_full_local_build_matches_national_contract_and_does_not_release'):
        'official checksum-pinned PDF cache is not installed',
    ('test_new_mexico_grade_evidence.NewMexicoGradeEvidenceTests.'
     'test_full_build_matches_national_contract_and_never_releases'):
        'official checksum-pinned PDF cache is not installed',
    ('test_south_dakota_grade_evidence.SouthDakotaGradeEvidenceTests.'
     'test_full_local_build_is_reproducible_and_does_not_publish'):
        'official checksum-pinned South Dakota PDF cache is not installed',
    ('test_utah_grade_evidence.UtahGradeEvidenceTests.'
     'test_two_full_local_builds_are_byte_identical_and_do_not_publish'):
        'official checksum-pinned Utah PDF cache is not installed',
}


# The geomodel format suite (tests/test_geomodel_*.py) always runs its own
# pure-Python round trips; on top of those it cross-checks the bytes against
# third-party readers (GDAL/rasterio, ezdxf, trimesh, segyio, lasio, pyarrow,
# the omf-rust ``omf2`` wheel, the ``omf`` 1.0.1 reference environment) and a
# few reference fixtures that are not vendored. Those extra comparisons are
# unavailable in a clean checkout, so they skip with this prefix. The prefix is
# the review: it names the class of skip, it never hides a repository test
# (the format assertions run regardless), and the count is reported separately
# below so an unexpected jump stays visible.
OPTIONAL_VALIDATOR_PREFIX = 'optional cross-validator unavailable: '


class StrictSkipResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.unreviewed_skips: list[tuple[str, str]] = []
        self.optional_validator_skips: list[tuple[str, str]] = []

    def addSkip(self, test, reason):  # noqa: N802 - unittest API
        super().addSkip(test, reason)
        test_id = test.id()
        if reason.startswith(OPTIONAL_VALIDATOR_PREFIX):
            self.optional_validator_skips.append((test_id, reason))
        elif ALLOWED_SKIPS.get(test_id) != reason:
            self.unreviewed_skips.append((test_id, reason))

    def wasSuccessful(self):  # noqa: N802 - unittest API
        return super().wasSuccessful() and not self.unreviewed_skips


class StrictSkipRunner(unittest.TextTestRunner):
    resultclass = StrictSkipResult


def main() -> int:
    os.chdir(PROJECT)
    sys.path.insert(0, str(PROJECT))
    # Some Python distributions install an unrelated top-level ``tests``
    # package. A few repository tests import shared fixtures as
    # ``tests.test_*``; bind that name to this checkout before discovery so
    # the suite cannot accidentally resolve third-party test helpers.
    tests_package = types.ModuleType('tests')
    tests_package.__path__ = [str(PROJECT / 'tests')]
    tests_package.__package__ = 'tests'
    sys.modules['tests'] = tests_package
    loader = unittest.TestLoader()
    suite = loader.discover('tests')
    if loader.errors:
        for error in loader.errors:
            print(error, file=sys.stderr)
        return 1
    result = StrictSkipRunner(verbosity=2).run(suite)
    if result.unreviewed_skips:
        print('\nUnreviewed unittest skips are forbidden:', file=sys.stderr)
        for test_id, reason in result.unreviewed_skips:
            print(f'  {test_id}: {reason}', file=sys.stderr)
    reviewed = (len(result.skipped) - len(result.optional_validator_skips)
                - len(result.unreviewed_skips))
    print(f'Reviewed source-cache skips: {reviewed}; '
          f'optional cross-validator skips: '
          f'{len(result.optional_validator_skips)}; '
          f'unreviewed skips: {len(result.unreviewed_skips)}')
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    raise SystemExit(main())
