#!/usr/bin/env python3
"""Fail-closed upload inventory and hygiene checks for the public site tree.

The public PMTiles/COG uploader must never infer authority from files that
happen to exist below ``site/data``.  This module instead derives the exact
local binary set from the public manifest, then rejects every additional
binary before deploy.sh can contact AWS.  It also builds the temporary public
manifest used for deployment: friendly local build paths become immutable,
content-addressed ``map-assets/baselines`` object keys.

Immutable ``site/map-assets/releases`` objects are deliberately outside this
allowlist.  They have a separate registry-gated validator and uploader.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import stat
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable


class PublicSiteError(ValueError):
    """Raised when the public site tree is unsafe or internally inconsistent."""


@dataclass(frozen=True)
class PublicBinary:
    path: str
    bytes: int
    sha256: str


@dataclass(frozen=True)
class DeploymentBinary:
    local_path: str
    object_path: str
    bytes: int
    sha256: str
    sha256_base64: str


@dataclass(frozen=True)
class DeploymentBundle:
    binaries: tuple[DeploymentBinary, ...]
    manifest: dict[str, Any]


_BINARY_SUFFIXES = ('.pmtiles', '.tif', '.tiff')
_IMMUTABLE_RELEASE_PREFIX = PurePosixPath('map-assets/releases')
_BASELINE_DEPLOYMENT_PREFIX = PurePosixPath('map-assets/baselines')
_PUBLIC_BINARY_REFERENCE = re.compile(
    r"(?P<path>(?:data|map-assets/baselines)/"
    r"[A-Za-z0-9][A-Za-z0-9._/-]*\.(?:pmtiles|tif|tiff))",
    re.IGNORECASE,
)
_SAFE_PATH_PART = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]*')
_TEMP_SUFFIXES = (
    '.bak', '.backup', '.old', '.orig', '.rej', '.swp', '.swo',
    '.temp', '.tmp',
)
_TEMP_NAMES = {
    'backup', 'backups', 'temp', 'temporary', 'tmp',
}
_SENSITIVE_SUFFIXES = {
    '.credentials', '.db', '.dump', '.env', '.jks', '.kdbx', '.key', '.keystore',
    '.p12', '.pem', '.pfx', '.ppk', '.secret', '.secrets', '.sql',
    '.sqlite', '.sqlite3', '.tfstate',
}
_SENSITIVE_NAMES = {
    'authorized_keys', 'credentials', 'credentials.json', 'id_dsa',
    'id_ecdsa', 'id_ed25519', 'id_rsa', 'secrets.json',
    'service-account.json', 'service_account.json', 'terraform.tfstate',
}


def _strict_json(path: Path) -> Any:
    def object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise PublicSiteError(f'{path}: duplicate JSON key {key!r}')
            result[key] = value
        return result

    def reject_constant(value: str) -> Any:
        raise PublicSiteError(f'{path}: non-finite JSON value {value}')

    try:
        payload = json.loads(
            path.read_text(encoding='utf-8'),
            object_pairs_hook=object_pairs,
            parse_constant=reject_constant,
        )
    except PublicSiteError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PublicSiteError(f'could not read strict JSON {path}: {exc}') from exc
    return payload


def _relative(path: Path, site: Path) -> str:
    return path.relative_to(site).as_posix()


def _is_binary_path(value: str) -> bool:
    return value.lower().endswith(_BINARY_SUFFIXES)


def _canonical_data_binary(value: str, *, source: str) -> str:
    if '\\' in value or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise PublicSiteError(f'{source}: unsafe binary path {value!r}')
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or path.parts[0] != 'data':
        raise PublicSiteError(f'{source}: public mutable binary must be below data/: {value!r}')
    if any(part in ('', '.', '..') or part.startswith('.') for part in path.parts):
        raise PublicSiteError(f'{source}: non-canonical public binary path {value!r}')
    if any(_SAFE_PATH_PART.fullmatch(part) is None for part in path.parts):
        raise PublicSiteError(f'{source}: unsafe public binary path component in {value!r}')
    if value != path.as_posix():
        raise PublicSiteError(f'{source}: non-canonical public binary path {value!r}')
    if path.suffix not in _BINARY_SUFFIXES:
        raise PublicSiteError(f'{source}: unsupported public binary path {value!r}')
    return path.as_posix()


def _descriptor_identity(node: dict[str, Any], source: str) -> tuple[int, str] | None:
    has_bytes = 'bytes' in node
    has_sha = 'sha256' in node
    if not has_bytes and not has_sha:
        return None
    if not has_bytes or not has_sha:
        raise PublicSiteError(
            f'{source}: binary descriptor must provide both bytes and sha256')
    size = node['bytes']
    digest = node['sha256']
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        raise PublicSiteError(f'{source}: invalid binary descriptor byte count')
    if not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest):
        raise PublicSiteError(f'{source}: invalid binary descriptor sha256')
    return size, digest


def _manifest_binary_references(
        value: Any,
        *,
        source: str = 'manifest',
) -> dict[str, tuple[int, str] | None]:
    references: dict[str, tuple[int, str] | None] = {}

    def reference_path(value: str, location: str) -> str | None:
        if value.startswith('data/') and _is_binary_path(value):
            return _canonical_data_binary(value, source=location)
        if value.startswith('pmtiles://'):
            local_path = value[len('pmtiles://'):]
            if local_path.startswith('data/') and _is_binary_path(local_path):
                return _canonical_data_binary(local_path, source=location)
        embedded = _PUBLIC_BINARY_REFERENCE.search(value)
        if embedded:
            if embedded.group('path').startswith('map-assets/baselines/'):
                raise PublicSiteError(
                    f'{location}: content-addressed baseline paths are generated '
                    'only in the temporary deployment manifest')
            raise PublicSiteError(
                f'{location}: mutable binary path must be an exact string or exact '
                'pmtiles:// URI (query strings and substrings are forbidden)')
        return None

    def add(path: str, identity: tuple[int, str] | None, location: str) -> None:
        previous = references.get(path)
        if path in references and previous is not None and identity is not None \
                and previous != identity:
            raise PublicSiteError(
                f'{location}: conflicting identity metadata for {path}')
        if path not in references or previous is None:
            references[path] = identity

    def walk(node: Any, location: str) -> None:
        if isinstance(node, dict):
            has_local_binary = any(
                isinstance(child, str)
                and reference_path(child, f'{location}.{key}') is not None
                for key, child in node.items()
            )
            identity = _descriptor_identity(node, location) \
                if has_local_binary else None
            for key, child in node.items():
                child_location = f'{location}.{key}'
                if isinstance(child, str):
                    binary_path = reference_path(child, child_location)
                    if binary_path is not None:
                        add(binary_path, identity, child_location)
                walk(child, child_location)
        elif isinstance(node, list):
            for index, child in enumerate(node):
                walk(child, f'{location}[{index}]')

    walk(value, source)
    return references


def _ui_binary_references(index_text: str) -> set[str]:
    return {
        match.group('path')
        for match in _PUBLIC_BINARY_REFERENCE.finditer(index_text)
    }


def _temporary_reason(name: str) -> str | None:
    lower = name.lower()
    if name.startswith('.'):
        return 'hidden'
    if lower in _TEMP_NAMES or lower.endswith(_TEMP_SUFFIXES) or name.endswith('~'):
        return 'temporary/backup'
    if re.search(r'(^|[._-])(bak|backup|temp|tmp)([._-]|$)', lower):
        return 'temporary/backup'
    if lower.startswith('~$') or (name.startswith('#') and name.endswith('#')):
        return 'temporary/backup'
    return None


def _sensitive_reason(name: str) -> str | None:
    lower = name.lower()
    if lower == '.env' or lower.startswith('.env.'):
        return 'sensitive'
    if lower in _SENSITIVE_NAMES:
        return 'sensitive'
    if any(lower.endswith(suffix) for suffix in _SENSITIVE_SUFFIXES):
        return 'sensitive'
    if any(lower.startswith(f'{stem}.') for stem in (
            'id_dsa', 'id_ecdsa', 'id_ed25519', 'id_rsa')):
        return 'sensitive'
    return None


def _inspect_public_tree(site: Path) -> list[str]:
    errors: list[str] = []
    if site.is_symlink():
        return [f'public site root must not be a symlink: {site}']
    if not site.is_dir():
        return [f'public site root is missing or not a directory: {site}']

    for directory, dirnames, filenames in os.walk(site, topdown=True, followlinks=False):
        directory_path = Path(directory)
        entries = sorted((*dirnames, *filenames))
        for name in entries:
            path = directory_path / name
            relative = _relative(path, site)
            reason = _temporary_reason(name) or _sensitive_reason(name)
            if reason:
                errors.append(f'{reason} public path is forbidden: {relative}')
            try:
                mode = path.lstat().st_mode
            except OSError as exc:
                errors.append(f'could not inspect public path {relative}: {exc}')
                continue
            if stat.S_ISLNK(mode):
                errors.append(f'public symlinks are forbidden: {relative}')
                if name in dirnames:
                    dirnames.remove(name)
            elif not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
                errors.append(f'non-file public path is forbidden: {relative}')
        # os.walk accepts in-place pruning only through dirnames.
        dirnames[:] = sorted(dirnames)
    return errors


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _actual_public_binaries(site: Path) -> set[str]:
    actual: set[str] = set()
    for directory, dirnames, filenames in os.walk(site, topdown=True, followlinks=False):
        directory_path = Path(directory)
        relative_directory = PurePosixPath(_relative(directory_path, site)) \
            if directory_path != site else PurePosixPath()
        if relative_directory == _IMMUTABLE_RELEASE_PREFIX:
            dirnames[:] = []
            continue
        for name in filenames:
            if _is_binary_path(name):
                actual.add(_relative(directory_path / name, site))
    return actual


def validate_public_site(site: str | os.PathLike[str]) -> dict[str, PublicBinary]:
    site_path = Path(site)
    errors = _inspect_public_tree(site_path)
    if errors:
        raise PublicSiteError('; '.join(sorted(set(errors))))

    manifest_path = site_path / 'data' / 'manifest.json'
    index_path = site_path / 'index.html'
    if not manifest_path.is_file():
        raise PublicSiteError('required public pointer is missing: data/manifest.json')
    if not index_path.is_file():
        raise PublicSiteError('required public UI is missing: index.html')

    manifest = _strict_json(manifest_path)
    if not isinstance(manifest, dict):
        raise PublicSiteError('data/manifest.json must contain a JSON object')
    manifest_references = _manifest_binary_references(manifest)
    try:
        ui_references = _ui_binary_references(index_path.read_text(encoding='utf-8'))
    except (OSError, UnicodeError) as exc:
        raise PublicSiteError(f'could not read public UI {index_path}: {exc}') from exc
    if ui_references:
        raise PublicSiteError(
            'index.html contains mutable binary path(s); all public tile paths '
            f'must resolve through the deployment manifest: {", ".join(sorted(ui_references))}')
    expected = set(manifest_references)
    actual = _actual_public_binaries(site_path)

    missing = sorted(expected - actual)
    unexpected = sorted(actual - expected)
    if missing or unexpected:
        details: list[str] = []
        if missing:
            details.append(f'missing referenced public binaries: {", ".join(missing)}')
        if unexpected:
            details.append(f'unexpected public binaries: {", ".join(unexpected)}')
        raise PublicSiteError('; '.join(details))

    result: dict[str, PublicBinary] = {}
    for relative in sorted(expected):
        target = site_path.joinpath(*PurePosixPath(relative).parts)
        if target.is_symlink() or not target.is_file():
            raise PublicSiteError(f'public binary is not a regular file: {relative}')
        size = target.stat().st_size
        digest = _hash(target)
        identity = manifest_references.get(relative)
        if identity is not None:
            expected_size, expected_digest = identity
            if size != expected_size:
                raise PublicSiteError(
                    f'public binary byte count mismatch for {relative}: '
                    f'manifest {expected_size}, local {size}')
            if digest != expected_digest:
                raise PublicSiteError(
                    f'public binary SHA-256 mismatch for {relative}: '
                    f'manifest {expected_digest}, local {digest}')
        result[relative] = PublicBinary(relative, size, digest)
    return result


def _deployment_key(binary: PublicBinary) -> str:
    suffix = PurePosixPath(binary.path).suffix
    if suffix not in _BINARY_SUFFIXES:
        raise PublicSiteError(
            f'cannot derive deployment suffix for public binary {binary.path}')
    return (_BASELINE_DEPLOYMENT_PREFIX / f'{binary.sha256}{suffix}').as_posix()


def _assert_browser_descriptor_equality(value: Any, *, source: str) -> None:
    def walk(node: Any, location: str) -> None:
        if isinstance(node, dict):
            if 'browser_descriptor' in node:
                browser = node['browser_descriptor']
                if not isinstance(browser, dict):
                    raise PublicSiteError(
                        f'{location}.browser_descriptor must be an object')
                parent_file = node.get('file')
                browser_file = browser.get('file')
                if not isinstance(parent_file, str) or browser_file != parent_file:
                    raise PublicSiteError(
                        f'{location}: browser descriptor file must equal its parent file')
                if parent_file.lower().endswith('.pmtiles'):
                    expected_protocol = f'pmtiles://{parent_file}'
                    if browser.get('protocol_url') != expected_protocol:
                        raise PublicSiteError(
                            f'{location}: browser descriptor protocol_url must equal '
                            f'{expected_protocol!r}')
                for key in ('bounds', 'maxzoom', 'minzoom'):
                    if key in node and key in browser and node[key] != browser[key]:
                        raise PublicSiteError(
                            f'{location}: browser descriptor {key} must equal its parent')
            for key, child in node.items():
                walk(child, f'{location}.{key}')
        elif isinstance(node, list):
            for index, child in enumerate(node):
                walk(child, f'{location}[{index}]')

    walk(value, source)


def _rewrite_deployment_manifest(
        manifest: dict[str, Any],
        mappings: dict[str, str],
) -> dict[str, Any]:
    rewritten_counts = {path: 0 for path in mappings}

    def rewrite(node: Any, location: str) -> Any:
        if isinstance(node, dict):
            return {
                key: rewrite(child, f'{location}.{key}')
                for key, child in node.items()
            }
        if isinstance(node, list):
            return [
                rewrite(child, f'{location}[{index}]')
                for index, child in enumerate(node)
            ]
        if not isinstance(node, str):
            return node
        if node in mappings:
            rewritten_counts[node] += 1
            return mappings[node]
        if node.startswith('pmtiles://'):
            local_path = node[len('pmtiles://'):]
            if local_path in mappings:
                rewritten_counts[local_path] += 1
                return f'pmtiles://{mappings[local_path]}'
        embedded = sorted(path for path in mappings if path in node)
        if embedded:
            raise PublicSiteError(
                f'{location}: mutable binary path must be an exact string or exact '
                f'pmtiles:// URI (query strings and substrings are forbidden): '
                f'{", ".join(embedded)}')
        return node

    rewritten = rewrite(manifest, 'manifest')
    if not isinstance(rewritten, dict):  # defensive; the public manifest is an object.
        raise PublicSiteError('rewritten deployment manifest is not an object')
    missing = sorted(path for path, count in rewritten_counts.items() if count == 0)
    if missing:
        raise PublicSiteError(
            f'deployment manifest did not rewrite all public binaries: {", ".join(missing)}')
    return rewritten


def _assert_exact_manifest_rewrite(
        original: Any,
        rewritten: Any,
        mappings: dict[str, str],
        *,
        source: str = 'manifest',
) -> None:
    def expected_string(value: str) -> str:
        if value in mappings:
            return mappings[value]
        if value.startswith('pmtiles://'):
            local_path = value[len('pmtiles://'):]
            if local_path in mappings:
                return f'pmtiles://{mappings[local_path]}'
        return value

    def compare(before: Any, after: Any, location: str) -> None:
        if type(before) is not type(after):
            raise PublicSiteError(f'{location}: deployment rewrite changed value type')
        if isinstance(before, dict):
            if list(before) != list(after):
                raise PublicSiteError(f'{location}: deployment rewrite changed object keys')
            for key in before:
                compare(before[key], after[key], f'{location}.{key}')
        elif isinstance(before, list):
            if len(before) != len(after):
                raise PublicSiteError(f'{location}: deployment rewrite changed list length')
            for index, (left, right) in enumerate(zip(before, after)):
                compare(left, right, f'{location}[{index}]')
        elif isinstance(before, str):
            if after != expected_string(before):
                raise PublicSiteError(f'{location}: deployment rewrite was not exact')
        elif before != after:
            raise PublicSiteError(
                f'{location}: deployment rewrite changed non-path metadata')

    def reject_friendly(node: Any, location: str) -> None:
        if isinstance(node, dict):
            for key, child in node.items():
                reject_friendly(child, f'{location}.{key}')
        elif isinstance(node, list):
            for index, child in enumerate(node):
                reject_friendly(child, f'{location}[{index}]')
        elif isinstance(node, str):
            leaked = sorted(path for path in mappings if path in node)
            if leaked:
                raise PublicSiteError(
                    f'{location}: rewritten manifest retains friendly path(s): '
                    f'{", ".join(leaked)}')

    compare(original, rewritten, source)
    reject_friendly(rewritten, source)


def build_deployment_bundle(
        site: str | os.PathLike[str],
) -> DeploymentBundle:
    """Return a deduplicated immutable upload plan and rewritten manifest."""
    site_path = Path(site)
    allowlist = validate_public_site(site_path)
    manifest = _strict_json(site_path / 'data' / 'manifest.json')
    if not isinstance(manifest, dict):
        raise PublicSiteError('data/manifest.json must contain a JSON object')
    _assert_browser_descriptor_equality(manifest, source='manifest')

    mappings = {
        local_path: _deployment_key(binary)
        for local_path, binary in allowlist.items()
    }
    rewritten = _rewrite_deployment_manifest(manifest, mappings)
    _assert_exact_manifest_rewrite(manifest, rewritten, mappings)
    _assert_browser_descriptor_equality(rewritten, source='deployment manifest')

    unique: dict[str, DeploymentBinary] = {}
    for local_path, binary in allowlist.items():
        object_path = mappings[local_path]
        item = DeploymentBinary(
            local_path=local_path,
            object_path=object_path,
            bytes=binary.bytes,
            sha256=binary.sha256,
            sha256_base64=base64.b64encode(
                bytes.fromhex(binary.sha256)).decode('ascii'),
        )
        previous = unique.get(object_path)
        if previous is not None:
            if (previous.bytes, previous.sha256) != (item.bytes, item.sha256):
                raise PublicSiteError(
                    f'conflicting local identities map to deployment key {object_path}')
            continue
        unique[object_path] = item
    return DeploymentBundle(
        binaries=tuple(unique[path] for path in sorted(unique)),
        manifest=rewritten,
    )


def _write_deployment_manifest(path: str | os.PathLike[str], manifest: dict[str, Any]) -> None:
    target = Path(path)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    if hasattr(os, 'O_NOFOLLOW'):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(target, flags, 0o600)
        with os.fdopen(descriptor, 'w', encoding='utf-8') as destination:
            json.dump(manifest, destination, ensure_ascii=False, indent=2)
            destination.write('\n')
    except OSError as exc:
        raise PublicSiteError(
            f'could not write temporary deployment manifest {target}: {exc}') from exc


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description='Validate the public tree and derive its exact mutable binary allowlist.')
    parser.add_argument(
        '--site', default=str(Path(__file__).resolve().parent.parent / 'site'),
        help='public site root (default: repository site/)')
    parser.add_argument(
        '--format', choices=('json', 'paths', 'deployment'), default='json',
        help=('JSON report, friendly local paths, or a tab-separated immutable '
              'deployment upload plan'))
    parser.add_argument(
        '--manifest-output',
        help='required with --format deployment; strict rewritten manifest destination')
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.format == 'deployment' and not args.manifest_output:
        print('ERROR: --manifest-output is required with --format deployment',
              file=sys.stderr)
        return 2
    if args.format != 'deployment' and args.manifest_output:
        print('ERROR: --manifest-output is only valid with --format deployment',
              file=sys.stderr)
        return 2
    try:
        if args.format == 'deployment':
            bundle = build_deployment_bundle(args.site)
            _write_deployment_manifest(args.manifest_output, bundle.manifest)
        else:
            allowlist = validate_public_site(args.site)
    except PublicSiteError as exc:
        print(f'ERROR: public site validation failed: {exc}', file=sys.stderr)
        return 1
    if args.format == 'deployment':
        for item in bundle.binaries:
            print('\t'.join((
                item.local_path,
                item.object_path,
                str(item.bytes),
                item.sha256,
                item.sha256_base64,
            )))
    elif args.format == 'paths':
        for relative in allowlist:
            print(relative)
    else:
        print(json.dumps({
            'status': 'pass',
            'public_data_binaries': [
                {
                    'path': item.path,
                    'bytes': item.bytes,
                    'sha256': item.sha256,
                }
                for item in allowlist.values()
            ],
            'count': len(allowlist),
        }, indent=2, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
