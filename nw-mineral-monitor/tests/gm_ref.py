"""Where the geomodel cross-check reference corpus lives.

The reference files are third-party artifacts (omf-rust's OMF v2 sample, the
LaGriT GOCAD samples, harmonica's Oasis montaj reader, a reference-written OMF
v0.9 file) that are fetched, not vendored.  ``tools/fetch_gm_refs.py`` installs
them; this module is the single place that knows where they land, so no test
carries an absolute path to whichever machine last built the corpus.

Override the location with ``GM_REF_DIR``, and the OMF v0.9 reference
interpreter with ``GM_OMF1_PYTHON``.
"""

import os
from pathlib import Path

DEFAULT_REF_DIR = Path.home() / '.cache' / 'nw-mineral-monitor' / 'gm-ref'
# The corpus used to be built in a container at this path; honour it so an
# existing checkout keeps cross-checking without re-fetching.
_LEGACY_REF_DIR = Path('/home/claude/ref')


def ref_dir():
    env = os.environ.get('GM_REF_DIR')
    if env:
        return Path(env).expanduser()
    if not DEFAULT_REF_DIR.exists() and _LEGACY_REF_DIR.exists():
        return _LEGACY_REF_DIR
    return DEFAULT_REF_DIR


REF = ref_dir()

OMF_RUST_GIT = REF / 'omf-rust-git'
SAMPLE_V2 = OMF_RUST_GIT / 'tests' / 'one_of_everything.omf'
if not SAMPLE_V2.exists():
    SAMPLE_V2 = REF / 'omfrust' / 'one_of_everything.omf'
SAMPLE_V09 = REF / 'test_v09.omf'
GOCAD_DIR = REF / 'gocad'
DXF_DIR = REF / 'dxf'
HARMONICA_REF = REF / 'oasis_montaj_grd.py'


def omf1_python():
    """Interpreter of the venv holding the reference ``omf`` 1.0.1 package."""
    env = os.environ.get('GM_OMF1_PYTHON')
    if env:
        return Path(env).expanduser()
    local = REF / 'omfenv' / 'bin' / 'python'
    return local if local.exists() else Path('/tmp/omfenv/bin/python')
