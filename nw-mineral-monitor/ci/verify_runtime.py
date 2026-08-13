#!/usr/bin/env python3
"""Fail CI before discovery when a dependency-gated test would be skipped."""

from __future__ import annotations

import shutil
import subprocess

import fiona
import numpy
import shapely


EXPECTED_FIONA = '1.9.5'
EXPECTED_NUMPY = '1.26.4'
EXPECTED_SHAPELY = '2.0.3'
EXPECTED_GEOS = '3.11.3'
EXPECTED_TIPPECANOE = 'tippecanoe v2.79.0'
EXPECTED_GDAL_PREFIX = 'GDAL 3.8.'


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def main() -> None:
    require(fiona.__version__ == EXPECTED_FIONA,
            f'Fiona must be {EXPECTED_FIONA}, got {fiona.__version__}')
    require(numpy.__version__ == EXPECTED_NUMPY,
            f'NumPy must be {EXPECTED_NUMPY}, got {numpy.__version__}')
    require(shapely.__version__ == EXPECTED_SHAPELY,
            f'Shapely must be {EXPECTED_SHAPELY}, got {shapely.__version__}')
    require(shapely.geos_version_string == EXPECTED_GEOS,
            f'GEOS must be {EXPECTED_GEOS}, got {shapely.geos_version_string}')

    executable = shutil.which('tippecanoe')
    require(executable is not None, 'tippecanoe is absent from PATH')
    completed = subprocess.run(
        [executable, '--version'], check=True, capture_output=True, text=True)
    observed = (completed.stdout + completed.stderr).strip()
    require(observed == EXPECTED_TIPPECANOE,
            f'expected {EXPECTED_TIPPECANOE}, got {observed!r}')

    gdalinfo = shutil.which('gdalinfo')
    require(gdalinfo is not None, 'gdalinfo is absent from PATH')
    completed = subprocess.run(
        [gdalinfo, '--version'], check=True, capture_output=True, text=True)
    gdal_version = (completed.stdout + completed.stderr).strip()
    require(gdal_version.startswith(EXPECTED_GDAL_PREFIX),
            f'expected {EXPECTED_GDAL_PREFIX}x, got {gdal_version!r}')
    print('CI runtime verified: Fiona 1.9.5, NumPy 1.26.4, Shapely 2.0.3 '
          '/ GEOS 3.11.3, Tippecanoe v2.79.0, GDAL 3.8.x')


if __name__ == '__main__':
    main()
