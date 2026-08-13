#!/usr/bin/env python3
"""Verify every checked-out tiled binary is materialized from Git LFS."""

from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess


PROJECT = Path(__file__).resolve().parents[1]
REPO = PROJECT.parent
SITE = PROJECT / 'site'
MANIFEST = SITE / 'data' / 'manifest.json'
SUFFIXES = ('.pmtiles', '.tif', '.tiff')
LFS_POINTER_PREFIX = b'version https://git-lfs.github.com/spec/v1\n'
PMTILES_MAGIC = b'PMTiles\x03'
TIFF_MAGICS = (b'II*\x00', b'MM\x00*', b'II+\x00', b'MM\x00+')
HTML_ASSET = re.compile(
    r'''["'](?:pmtiles://)?(data/[^"']+\.(?:pmtiles|tif|tiff))["']''',
    re.IGNORECASE)


def _manifest_asset_strings(value):
    if isinstance(value, dict):
        for key, child in value.items():
            if (key == 'file' and isinstance(child, str) and
                    child.lower().endswith(SUFFIXES)):
                yield child
            yield from _manifest_asset_strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _manifest_asset_strings(child)


def _site_asset(relative: str, site: Path = SITE) -> Path:
    if not relative.startswith(('data/', 'map-assets/releases/')):
        raise RuntimeError(f'tiled asset is outside public asset roots: {relative}')
    candidate = (site / relative).resolve()
    site_root = site.resolve()
    if candidate == site_root or site_root not in candidate.parents:
        raise RuntimeError(f'tiled asset escapes the site root: {relative}')
    return candidate


def expected_assets(project: Path = PROJECT) -> set[Path]:
    site = project / 'site'
    manifest_path = site / 'data' / 'manifest.json'
    with manifest_path.open(encoding='utf-8') as source:
        manifest = json.load(source)
    expected = {
        _site_asset(relative, site)
        for relative in _manifest_asset_strings(manifest)
    }
    index = (site / 'index.html').read_text(encoding='utf-8')
    expected.update(_site_asset(match, site)
                    for match in HTML_ASSET.findall(index))
    states = project / 'states'
    if states.is_dir():
        for state_path in sorted(states.glob('[A-Z][A-Z].yaml')):
            with state_path.open(encoding='utf-8') as source:
                state = json.load(source)
            for value in _all_strings(state):
                if (value.startswith('map-assets/releases/') and
                        value.lower().endswith(SUFFIXES)):
                    expected.add(_site_asset(value, site))
    return expected


def _all_strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from _all_strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _all_strings(child)


def tiled_files(root: Path) -> set[Path]:
    return {path.resolve() for path in root.rglob('*')
            if path.is_file() and path.suffix.lower() in SUFFIXES}


def validate_materialized_asset(path: Path) -> None:
    if path.is_symlink():
        raise RuntimeError(f'tiled asset must not be a symlink: {path}')
    if not path.is_file():
        raise RuntimeError(f'tiled asset is missing: {path}')
    with path.open('rb') as source:
        header = source.read(max(len(LFS_POINTER_PREFIX), len(PMTILES_MAGIC)))
    if header.startswith(LFS_POINTER_PREFIX):
        raise RuntimeError(f'Git LFS pointer stub was not materialized: {path}')
    suffix = path.suffix.lower()
    if suffix == '.pmtiles' and not header.startswith(PMTILES_MAGIC):
        raise RuntimeError(f'PMTiles v3 magic is absent: {path}')
    if suffix in ('.tif', '.tiff') and not header.startswith(TIFF_MAGICS):
        raise RuntimeError(f'TIFF/BigTIFF magic is absent: {path}')


def _git_lines(*arguments: str) -> list[str]:
    completed = subprocess.run(
        ['git', '-C', str(REPO), *arguments], check=True,
        capture_output=True, text=True)
    return [line for line in completed.stdout.splitlines() if line]


def _repo_relative(path: Path) -> str:
    return path.resolve().relative_to(REPO.resolve()).as_posix()


def main() -> None:
    expected = expected_assets()
    present = tiled_files(SITE)
    missing = sorted(_repo_relative(path) for path in expected - present)
    if missing:
        raise RuntimeError(f'manifest/UI tiled assets are missing: {missing}')
    unreferenced = sorted(_repo_relative(path) for path in present - expected)
    if unreferenced:
        raise RuntimeError(
            f'public tiled assets are not referenced by manifest/UI/registry: '
            f'{unreferenced}')

    tracked = {
        (REPO / relative).resolve()
        for relative in _git_lines('ls-files')
        if relative.lower().endswith(SUFFIXES)
    }
    untracked = sorted(_repo_relative(path) for path in present - tracked)
    if untracked:
        raise RuntimeError(f'tiled assets are not tracked by Git: {untracked}')

    lfs_paths = {
        (REPO / relative).resolve()
        for relative in _git_lines('lfs', 'ls-files', '--name-only')
    }
    non_lfs = sorted(_repo_relative(path) for path in tracked - lfs_paths)
    if non_lfs:
        raise RuntimeError(f'tiled binaries are not Git LFS objects: {non_lfs}')

    for path in sorted(present | tracked):
        validate_materialized_asset(path)
    total_bytes = sum(path.stat().st_size for path in present)
    print(f'Git LFS checkout verified: {len(present)} materialized tiled '
          f'assets, {total_bytes} bytes')


if __name__ == '__main__':
    main()
