#!/usr/bin/env python3
"""Install the geomodel cross-check reference corpus.

The geomodel format readers and writers are validated against other people's
implementations -- GDAL, ezdxf, trimesh, segyio, lasio, pyarrow, harmonica,
the reference ``omf`` 1.0.1 writer and Seequent's ``omf-rust``.  Those
reference files are third-party artifacts, so they are fetched from pinned
upstream commits and checksum-verified here rather than vendored into the
repository.

    pip install -r ci/requirements-crosscheck.txt
    python3 tools/fetch_gm_refs.py

Everything lands in ``$GM_REF_DIR`` (default ``~/.cache/nw-mineral-monitor/
gm-ref``).  The script is idempotent: an artifact whose checksum already
matches is left alone, so re-running it costs nothing.  Without the corpus the
cross-checks skip, and ``ci/run_tests.py`` fails the run -- unreviewed skips
are forbidden.

``--build-omf2`` additionally builds the ``omf2`` wheel from omf-rust, which
needs a Rust toolchain (https://rustup.rs).  Three OMF tests depend on it.
"""

from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import venv


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tests'))
from gm_ref import ref_dir                                          # noqa: E402


# Pinned upstream commits.  Bump deliberately: the checksums below are the
# real gate, and a bump that changes a reference file must change them too.
SOURCES = {
    'omf-rust': ('https://github.com/gmggroup/omf-rust',
                 '64d305790b3415618a01f00a94cd914acd0b80ba'),
    'lagrit': ('https://github.com/lanl/LaGriT',
               'ace1e7f89d50ccf9b603d67c82d20002e1fb89ed'),
    'harmonica': ('https://github.com/fatiando/harmonica',
                  '983051e7cfc1fcdb4e82719ae12418849eda08ad'),
    'cfm': ('https://github.com/SCECcode/cfm',
            '6d09bd20987b2cec606beda4c336f2ec175fb4b3'),
}

# (source, path in that repo, destination under the ref dir, sha256)
FILES = [
    ('lagrit', 'test/level01/read_gocad/input_3tri_all_props.ts', 'gocad/input_3tri_all_props.ts',
     '8a004e3ad694c12f37f24580c087e67ff3e3d9294a2cd82a0934154a3993ba8f'),
    ('lagrit', 'test/level01/read_gocad/input_3tri_node_props.ts', 'gocad/input_3tri_node_props.ts',
     'e39cd21143bf31d2e8e07b1bac2e2142288643c48d690c5d77ddd020a3aaf29e'),
    ('lagrit', 'test/level01/read_gocad/input_small_TFACE.ts', 'gocad/input_small_TFACE.ts',
     '74de3b2c64fa4850ed8e047308fce1ea812d261af2b0bce3458f4e94465ee86f'),
    ('cfm', 'CFM5_release_2017/obj/CFM5_all/MJVA-GLPS-GLDS-Goldstone_Lake_fault-CFM1.ts', 'gocad/cfm.ts',
     '012a83eadb0999518ef4cc9cd3aa5f0aa672c74fbebab37887bc12221d3d7761'),
    ('harmonica', 'src/harmonica/_io/oasis_montaj_grd.py', 'oasis_montaj_grd.py',
     '35a30102fca73b2b2e06375b38cebb19e3539d16391cb2b1c822bda65a1a77d2'),
]

# omf-rust is kept as a working tree, not a single file: the OMF v2 tests read
# tests/one_of_everything.omf out of it and --build-omf2 builds omf-python.
OMF_RUST_SENTINEL = ('tests/one_of_everything.omf',
                     '4bc9af3060ce25e929901d6df921c3b5b0d4d5144b473dfb536b60a8ee36c2e8')

OMF1_PIN = 'omf==1.0.1'


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, 'rb') as fh:
        for block in iter(lambda: fh.read(1 << 20), b''):
            h.update(block)
    return h.hexdigest()


def run(cmd, **kw):
    kw.setdefault('check', True)
    kw.setdefault('stdout', subprocess.DEVNULL)
    return subprocess.run(cmd, **kw)


def pinned_checkout(name: str, cache: Path, paths: list[str]) -> Path:
    """Fetch exactly ``paths`` at the pinned commit -- one commit, no history,
    and for a partial clone no blobs beyond what is checked out.  The SCEC CFM
    repository is ~1 GB; a full clone for one 2 kB fault surface is not on."""
    url, sha = SOURCES[name]
    work = cache / name
    work.mkdir(parents=True, exist_ok=True)
    if not (work / '.git').exists():
        run(['git', 'init', '-q', str(work)])
        run(['git', '-C', str(work), 'remote', 'add', 'origin', url])
    head = subprocess.run(['git', '-C', str(work), 'rev-parse', '-q', '--verify', 'HEAD'],
                          capture_output=True, text=True)
    if head.stdout.strip() != sha:
        print('  fetching %s @ %s' % (url, sha[:12]))
        run(['git', '-C', str(work), 'fetch', '-q', '--depth', '1', '--filter=blob:none',
             'origin', sha])
    # --no-cone matches exact paths.  Cone mode would materialise whole
    # directories, and CFM5_all alone is hundreds of megabytes.
    run(['git', '-C', str(work), 'sparse-checkout', 'set', '--no-cone'] + ['/' + p for p in paths])
    if head.stdout.strip() != sha:
        run(['git', '-C', str(work), 'checkout', '-q', sha])
    return work


def install_files(ref: Path, cache: Path) -> int:
    fetched = 0
    by_source: dict[str, list[tuple[str, str, str]]] = {}
    for source, src, dest, digest in FILES:
        by_source.setdefault(source, []).append((src, dest, digest))
    for source, items in by_source.items():
        wanted = [(s, d, h) for s, d, h in items
                  if not (ref / d).exists() or sha256(ref / d) != h]
        if not wanted:
            print('  %s: up to date' % source)
            continue
        work = pinned_checkout(source, cache, [s for s, _, _ in wanted])
        for src, dest, digest in wanted:
            out = ref / dest
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(work / src, out)
            got = sha256(out)
            if got != digest:
                raise SystemExit(
                    'checksum mismatch for %s\n  expected %s\n  got      %s\n'
                    'The pinned upstream commit no longer produces this file. Review the '
                    'change upstream, then update FILES in this script.' % (dest, digest, got))
            print('  %s -> %s' % (src, dest))
            fetched += 1
    return fetched


def install_omf_rust(ref: Path, cache: Path) -> None:
    dest = ref / 'omf-rust-git'
    sentinel, digest = OMF_RUST_SENTINEL
    if (dest / sentinel).exists() and sha256(dest / sentinel) == digest:
        print('  omf-rust: up to date')
        return
    url, sha = SOURCES['omf-rust']
    if dest.exists():
        shutil.rmtree(dest)
    print('  cloning %s @ %s' % (url, sha[:12]))
    run(['git', 'clone', '-q', '--depth', '1', url, str(dest)])
    if subprocess.run(['git', '-C', str(dest), 'rev-parse', 'HEAD'],
                      capture_output=True, text=True).stdout.strip() != sha:
        run(['git', '-C', str(dest), 'fetch', '-q', '--depth', '1', 'origin', sha])
        run(['git', '-C', str(dest), 'checkout', '-q', sha])
    got = sha256(dest / sentinel)
    if got != digest:
        raise SystemExit('checksum mismatch for %s: expected %s, got %s' % (sentinel, digest, got))
    print('  omf-rust-git/%s' % sentinel)


def install_omf1_env(ref: Path) -> Path:
    """omf 1.0.1 is the reference OMF v0.9 implementation and pins packages
    (properties, vectormath) that must not touch the repository interpreter,
    so it gets a venv of its own."""
    env = ref / 'omfenv'
    python = env / 'bin' / 'python'
    if python.exists() and subprocess.run([str(python), '-c', 'import omf, numpy'],
                                          capture_output=True).returncode == 0:
        print('  omfenv: up to date')
        return python
    print('  building omfenv (%s)' % OMF1_PIN)
    venv.EnvBuilder(with_pip=True, clear=True).create(env)
    run([str(python), '-m', 'pip', 'install', '-q', '--upgrade', 'pip', 'setuptools', 'wheel'])
    run([str(python), '-m', 'pip', 'install', '-q', OMF1_PIN])
    run([str(python), '-c', 'import omf, numpy'])
    return python


def generate_omf1_reference(ref: Path, python: Path) -> None:
    out = ref / 'test_v09.omf'
    gen = Path(__file__).resolve().parent / 'gen_omf1_reference.py'
    if out.exists():
        print('  test_v09.omf: present')
        return
    print('  generating test_v09.omf with the reference writer')
    run([str(python), str(gen), str(out)])


def generate_dxf_reference(ref: Path) -> None:
    """A minimal R12 file written by ezdxf -- an independent producer -- with
    CRLF endings, which is what R12 in the wild actually uses.  The TEXT entity
    is there to be ignored: the reader must report it as skipped, not choke."""
    out = ref / 'dxf' / 'min_r12.dxf'
    if out.exists():
        print('  min_r12.dxf: present')
        return
    try:
        import ezdxf
    except ImportError:
        print('  min_r12.dxf: SKIPPED (pip install -r ci/requirements-crosscheck.txt)')
        return
    out.parent.mkdir(parents=True, exist_ok=True)
    doc = ezdxf.new('R12')
    msp = doc.modelspace()
    msp.add_3dface([(0, 0, 0), (1, 0, 0), (0, 1, 0), (0, 1, 0)], dxfattribs={'layer': 'TRI'})
    msp.add_polyline3d([(0, 0, 0), (1, 0, 0), (2, 0, 0)], dxfattribs={'layer': 'LINE'})
    msp.add_point((5, 5, 5), dxfattribs={'layer': 'PTS'})
    msp.add_text('skip me', dxfattribs={'layer': 'NOTES', 'height': 1.0}).set_placement((0, 0, 0))
    doc.saveas(out)
    raw = out.read_bytes()
    out.write_bytes(raw.replace(b'\r\n', b'\n').replace(b'\n', b'\r\n'))
    print('  dxf/min_r12.dxf')


def build_omf2(ref: Path) -> None:
    """omf2 is not published on PyPI; it is the maturin-built Python binding in
    omf-rust, so it needs cargo.  Three tests use it to prove our OMF v2 writer
    and our v0.9 -> v2 conversion are readable by Seequent's own reader."""
    if shutil.which('cargo') is None:
        print('  omf2: SKIPPED -- no Rust toolchain (https://rustup.rs), then re-run with --build-omf2')
        return
    src = ref / 'omf-rust-git' / 'omf-python'
    print('  building omf2 from %s' % src)
    run([sys.executable, '-m', 'pip', 'install', '-q', 'maturin>=1,<2'])
    run([sys.executable, '-m', 'pip', 'install', '-q', str(src)])
    run([sys.executable, '-c', 'import omf2'])
    print('  omf2 installed into %s' % sys.executable)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument('--build-omf2', action='store_true',
                    help='also build the omf2 wheel from omf-rust (needs cargo)')
    args = ap.parse_args()

    ref = ref_dir()
    ref.mkdir(parents=True, exist_ok=True)
    cache = ref / '.src'
    print('reference corpus: %s' % ref)

    install_files(ref, cache)
    install_omf_rust(ref, cache)
    python = install_omf1_env(ref)
    generate_omf1_reference(ref, python)
    generate_dxf_reference(ref)
    if args.build_omf2:
        build_omf2(ref)
    else:
        try:
            import omf2                                             # noqa: F401
            print('  omf2: already installed')
        except ImportError:
            print('  omf2: not installed -- re-run with --build-omf2 (needs cargo)')

    print('\nDone. Cross-checks run with:\n'
          "  python3 -m unittest discover -s tests -p 'test_geomodel_*.py'")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
