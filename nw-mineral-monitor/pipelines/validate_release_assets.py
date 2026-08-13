#!/usr/bin/env python3
"""Fail-closed allowlist validator for immutable WS11 release uploads.

Only an enabled state whose complete DONE gate passes may authorize a local
file below ``site/map-assets/releases``.  Every referenced artifact must carry
an exact byte count and SHA-256 in the registry, and its basename must be the
content digest.  The local release tree must then equal that derived allowlist
exactly: no orphan, hidden, temporary, symlink, or special paths are accepted.

The deploy script consumes ``--format paths`` and uploads each emitted path
individually.  Never broaden that interface into a recursive directory sync.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable, Mapping

from state_registry import (
    GATE_KEYS, load_states, release_file_descriptors, validate_registry)


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SITE = ROOT / 'site'
RELEASE_PREFIX = PurePosixPath('map-assets/releases')
ALLOWED_SUFFIXES = frozenset(('.json', '.pmtiles', '.tif', '.tiff'))
SHA256_RE = re.compile(r'[0-9a-f]{64}')
TEMP_NAMES = re.compile(
    r'(?:^#.*#$|~$|\.(?:bak|orig|part|swp|temp|tmp)$|'
    r'^(?:tmp|temp|temporary|stage|staging|build|build-inputs)$|'
    r'^(?:tmp|temp|staging)-|-(?:tmp|temp|staging)$)', re.IGNORECASE)


class ReleaseAssetError(ValueError):
    """The registry authorization or local release tree is not exact."""


@dataclass(frozen=True)
class ReleaseAsset:
    path: str
    sha256: str
    bytes: int
    states: tuple[str, ...]


def _canonical_release_path(raw_path, context: str) -> str:
    if not isinstance(raw_path, str) or not raw_path:
        raise ReleaseAssetError(f'{context}: artifact path must be nonempty text')
    if ('\\' in raw_path or '\n' in raw_path or '\r' in raw_path or
            any(ord(char) < 32 or ord(char) == 127 for char in raw_path)):
        raise ReleaseAssetError(f'{context}: artifact path contains forbidden characters')
    path = PurePosixPath(raw_path)
    if path.is_absolute() or str(path) != raw_path:
        raise ReleaseAssetError(f'{context}: artifact path is not canonical: {raw_path!r}')
    parts = path.parts
    if len(parts) <= len(RELEASE_PREFIX.parts) or parts[:2] != RELEASE_PREFIX.parts:
        raise ReleaseAssetError(
            f'{context}: artifact must be below {RELEASE_PREFIX.as_posix()}/')
    if any(part in ('', '.', '..') for part in parts):
        raise ReleaseAssetError(f'{context}: artifact path contains traversal')
    if any(part.startswith('.') for part in parts):
        raise ReleaseAssetError(f'{context}: hidden artifact paths are forbidden')
    if any(TEMP_NAMES.search(part) for part in parts):
        raise ReleaseAssetError(f'{context}: temporary artifact paths are forbidden')
    return path.as_posix()


def _release_gate_passes(code: str, row: Mapping) -> bool:
    release = row.get('release')
    if not isinstance(release, dict) or release.get('enabled') is not True:
        return False
    if release.get('status') != 'done':
        raise ReleaseAssetError(f'{code}: release.enabled=true requires release.status=done')
    gates = row.get('done_gate')
    if not isinstance(gates, dict) or set(gates) != set(GATE_KEYS):
        raise ReleaseAssetError(f'{code}: enabled release does not have the exact DONE gate')
    failed = sorted(
        key for key, gate in gates.items()
        if not isinstance(gate, dict) or gate.get('status') not in ('pass', 'not_applicable'))
    if failed:
        raise ReleaseAssetError(
            f'{code}: enabled release has incomplete DONE gates: {", ".join(failed)}')
    return True


def derive_allowlist(states: Mapping[str, Mapping]) -> dict[str, ReleaseAsset]:
    """Derive exact upload authorization from enabled, gate-passed state rows."""
    gathered: dict[str, dict] = {}
    errors = []
    for code in sorted(states):
        row = states[code]
        try:
            if not _release_gate_passes(code, row):
                continue
            references = list(release_file_descriptors(row))
            if not references:
                raise ReleaseAssetError(f'{code}: enabled release references no artifacts')
            for (context, path_container, path_field, metadata_container,
                 sha_field, bytes_field) in references:
                raw_path = path_container.get(path_field)
                path = _canonical_release_path(raw_path, f'{code}.{context}.{path_field}')
                sha256 = metadata_container.get(sha_field)
                size = metadata_container.get(bytes_field)
                descriptor_context = f'{code}.{context}'
                if sha256 is None:
                    errors.append(
                        f'{descriptor_context}: exact registry SHA-256 is missing')
                    continue
                if not isinstance(sha256, str) or SHA256_RE.fullmatch(sha256) is None:
                    errors.append(
                        f'{descriptor_context}: {sha_field} must be 64 lowercase '
                        'hexadecimal characters')
                    continue
                if size is None:
                    errors.append(
                        f'{descriptor_context}: exact registry byte count is missing')
                    continue
                if (not isinstance(size, int) or isinstance(size, bool) or size < 1):
                    errors.append(
                        f'{descriptor_context}: {bytes_field} must be a positive integer')
                    continue
                entry = gathered.setdefault(path, {
                    'sha256s': set(), 'sizes': set(), 'states': set(), 'contexts': []})
                entry['sha256s'].add(sha256)
                entry['sizes'].add(size)
                entry['states'].add(code)
                entry['contexts'].append(descriptor_context)
        except ReleaseAssetError as exc:
            errors.append(str(exc))
    if errors:
        raise ReleaseAssetError('\n'.join(errors))

    allowlist = {}
    for path, entry in sorted(gathered.items()):
        contexts = ', '.join(entry['contexts'])
        sha256s = entry['sha256s']
        sizes = entry['sizes']
        if len(sha256s) != 1:
            detail = 'missing' if not sha256s else f'conflicting: {sorted(sha256s)!r}'
            errors.append(f'{path}: exact registry SHA-256 is {detail} ({contexts})')
            continue
        if len(sizes) != 1:
            detail = 'missing' if not sizes else f'conflicting: {sorted(sizes)!r}'
            errors.append(f'{path}: exact registry byte count is {detail} ({contexts})')
            continue
        sha256 = next(iter(sha256s))
        size = next(iter(sizes))
        if not isinstance(sha256, str) or SHA256_RE.fullmatch(sha256) is None:
            errors.append(f'{path}: SHA-256 must be 64 lowercase hexadecimal characters')
            continue
        if not isinstance(size, int) or isinstance(size, bool) or size < 1:
            errors.append(f'{path}: byte count must be a positive integer')
            continue
        basename = PurePosixPath(path).name
        suffix = ''.join(PurePosixPath(path).suffixes)
        if suffix not in ALLOWED_SUFFIXES or basename != f'{sha256}{suffix}':
            errors.append(
                f'{path}: basename must be the declared SHA-256 plus one of '
                f'{sorted(ALLOWED_SUFFIXES)}')
            continue
        allowlist[path] = ReleaseAsset(
            path=path, sha256=sha256, bytes=size,
            states=tuple(sorted(entry['states'])))
    if errors:
        raise ReleaseAssetError('\n'.join(errors))
    return allowlist


def _path_name_forbidden(relative: PurePosixPath) -> bool:
    return any(part.startswith('.') or TEMP_NAMES.search(part)
               for part in relative.parts)


def _scan_release_tree(site: Path) -> tuple[set[str], list[str]]:
    release_root = site.joinpath(*RELEASE_PREFIX.parts)
    files: set[str] = set()
    directories: list[str] = []
    for prefix in (site / RELEASE_PREFIX.parts[0], release_root):
        if os.path.lexists(prefix) and prefix.is_symlink():
            raise ReleaseAssetError(f'{prefix}: release path must not be a symlink')
    if not os.path.lexists(release_root):
        return files, directories
    if not release_root.is_dir():
        raise ReleaseAssetError(f'{release_root}: release root must be a directory')
    for current, dirnames, filenames in os.walk(release_root, topdown=True,
                                                followlinks=False):
        current_path = Path(current)
        for name in sorted(dirnames + filenames):
            local = current_path / name
            relative_root = PurePosixPath(local.relative_to(release_root).as_posix())
            public = (RELEASE_PREFIX / relative_root).as_posix()
            try:
                mode = os.lstat(local).st_mode
            except OSError as exc:
                raise ReleaseAssetError(f'{public}: cannot inspect path: {exc}') from exc
            if stat.S_ISLNK(mode):
                raise ReleaseAssetError(f'{public}: symlinks are forbidden')
            if _path_name_forbidden(relative_root):
                raise ReleaseAssetError(f'{public}: hidden/temporary paths are forbidden')
            if stat.S_ISDIR(mode):
                directories.append(public)
            elif stat.S_ISREG(mode):
                files.add(public)
            else:
                raise ReleaseAssetError(f'{public}: special filesystem paths are forbidden')
    return files, directories


def _sha256_regular_file(path: Path) -> tuple[int, str]:
    flags = os.O_RDONLY
    if hasattr(os, 'O_NOFOLLOW'):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ReleaseAssetError(f'{path}: cannot open release artifact safely: {exc}') from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ReleaseAssetError(f'{path}: release artifact is not a regular file')
        digest = hashlib.sha256()
        while True:
            block = os.read(descriptor, 1024 * 1024)
            if not block:
                break
            digest.update(block)
        after = os.fstat(descriptor)
        if ((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) !=
                (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)):
            raise ReleaseAssetError(f'{path}: artifact changed while it was validated')
        return after.st_size, digest.hexdigest()
    finally:
        os.close(descriptor)


def validate_release_assets(site: Path, states: Mapping[str, Mapping]) -> dict[str, ReleaseAsset]:
    site = Path(os.path.abspath(site))
    allowlist = derive_allowlist(states)
    actual_files, actual_directories = _scan_release_tree(site)
    expected_files = set(allowlist)
    missing = sorted(expected_files - actual_files)
    unexpected = sorted(actual_files - expected_files)
    expected_directories = set()
    for public in expected_files:
        parent = PurePosixPath(public).parent
        while parent != RELEASE_PREFIX:
            expected_directories.add(parent.as_posix())
            parent = parent.parent
    unexpected_directories = sorted(set(actual_directories) - expected_directories)
    errors = []
    if missing:
        errors.append(f'missing authorized release files: {missing!r}')
    if unexpected:
        errors.append(f'unexpected release files: {unexpected!r}')
    if unexpected_directories:
        errors.append(f'unexpected release directories: {unexpected_directories!r}')
    for public in sorted(expected_files & actual_files):
        asset = allowlist[public]
        local = site.joinpath(*PurePosixPath(public).parts)
        try:
            size, digest = _sha256_regular_file(local)
        except ReleaseAssetError as exc:
            errors.append(str(exc))
            continue
        if size != asset.bytes:
            errors.append(
                f'{public}: bytes {size} != registry {asset.bytes}')
        if digest != asset.sha256:
            errors.append(
                f'{public}: SHA-256 {digest} != registry {asset.sha256}')
    if errors:
        raise ReleaseAssetError('\n'.join(errors))
    return allowlist


def _load_validated_states():
    result = validate_registry()
    if not result['ok']:
        raise ReleaseAssetError(
            'state registry is invalid:\n' + '\n'.join(result['errors']))
    return load_states(validate=True)


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--site', type=Path, default=DEFAULT_SITE)
    parser.add_argument('--format', choices=('json', 'paths'), default='json')
    args = parser.parse_args(argv)
    try:
        allowlist = validate_release_assets(args.site, _load_validated_states())
    except ReleaseAssetError as exc:
        print(f'ERROR: release-asset validation failed:\n{exc}', file=sys.stderr)
        return 1
    if args.format == 'paths':
        for path in sorted(allowlist):
            print(path)
    else:
        print(json.dumps({
            'ok': True,
            'released_states': sorted({state for asset in allowlist.values()
                                       for state in asset.states}),
            'files': len(allowlist),
            'bytes': sum(asset.bytes for asset in allowlist.values()),
            'allowlist': [asset.__dict__ for asset in allowlist.values()],
        }, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
