#!/usr/bin/env python3
"""Fail-closed gate for the WS12 document store and its citation contract.

This validator is offline and reads no PDF.  It checks that the committed
manifest is canonical and internally consistent, that every citation resolves
to a stored object without consulting the originating portal, that the
document corpus is not exposed through the public CloudFront prefix allowlist,
and that the deployed lifecycle matches what the manifest claims.

Passing this gate does not mean the objects are uploaded.  Remote presence is
proved only by ``deploy.sh upload-doc-store``, which re-verifies each object
after writing it.  Run with ``--store-dir`` to additionally hash-verify a
local generation.  The validator never writes, uploads, or enables anything.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

import doc_store


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
SITE = os.path.join(ROOT, 'site')
TEMPLATE = os.path.join(ROOT, 'infra', 'template.yaml')
DEFAULT_STORE = os.path.join(ROOT, 'pipelines', 'cache', 'ws12', 'store')
VIEWER = os.path.join(SITE, 'viewer.html')
LIFECYCLE_RULE_ID = 'ws12-raw-originals-to-ia'


class DocStoreGateError(ValueError):
    """The document store violates a WS12 delivery or privacy invariant."""


def _read_text(path, label):
    try:
        with open(path, 'r', encoding='utf-8') as source:
            return source.read()
    except OSError as exc:
        raise DocStoreGateError(f'cannot read {label}: {exc}') from exc


def check_private_delivery(template_text):
    """The corpus must never join the CloudFront-readable prefix allowlist.

    Only the bucket-policy statement is inspected.  The docs Lambda's own IAM
    role names the same prefix on purpose — that grant is what lets it sign a
    single object for one signed-in caller, and it is not public exposure.
    """
    problems = []
    marker = 'Sid: AllowCloudFrontRead'
    if marker not in template_text:
        return ['the CloudFront read statement is missing from the bucket policy']
    window = template_text.split(marker, 1)[1].split('Condition:', 1)[0]
    if '${SiteBucket.Arn}/docs/*' in window:
        problems.append(
            'the bucket policy exposes docs/* to CloudFront; the document '
            'corpus must stay private and be served by presigned GET only')
    if '${SiteBucket.Arn}/viewer.html' not in window:
        problems.append(
            'the bucket policy does not allow viewer.html, so the citation '
            'viewer would 403 from CloudFront')
    if ('Sid: DenyCloudFrontDocumentManifest' not in template_text or
            "${SiteBucket.Arn}/data/docs/manifest.json" not in template_text):
        problems.append(
            'the full document manifest is not explicitly denied to CloudFront')
    if "MANIFEST_KEY: 'private/ws12/document-store-manifest.json'" not in template_text:
        problems.append(
            'the Docs API is not configured to read the private source-of-truth manifest')
    return problems


def check_lifecycle(template_text, store):
    """The declared retention must match the lifecycle the stack deploys."""
    problems = []
    if f'Id: {LIFECYCLE_RULE_ID}' not in template_text:
        problems.append(
            f'template has no {LIFECYCLE_RULE_ID} lifecycle rule for docs/')
        return problems
    window = template_text.split(f'Id: {LIFECYCLE_RULE_ID}', 1)[1][:600]
    days = store['raw_transition_days']
    storage_class = store['raw_storage_class']
    if not re.search(rf'TransitionInDays:\s*{days}\b', window):
        problems.append(
            f'lifecycle rule does not transition raw originals after {days} days')
    if not re.search(rf'StorageClass:\s*{re.escape(storage_class)}\b', window):
        problems.append(
            f'lifecycle rule does not move raw originals to {storage_class}')
    if not re.search(r"Prefix:\s*'?docs/", window):
        problems.append('lifecycle rule is not scoped to the docs/ prefix')
    if not re.search(r"ObjectSizeGreaterThan:\s*['\"]?0['\"]?", window):
        problems.append(
            'lifecycle rule does not override the 128 KiB default for small raw originals')
    return problems


def check_portal_independence(manifest):
    """Every citation must resolve to a stored object, with no portal fetch.

    A citation that can only be honoured by following ``source_url`` dies with
    the portal.  Resolution here uses the manifest alone, so a passing run is
    the same evidence as blocking the source domain.
    """
    problems = []
    for row in manifest['citations']:
        try:
            resolved = doc_store.resolve_open_doc(
                manifest, row['doc_id'], row['page'], row['quote'])
        except doc_store.DocStoreError as exc:
            problems.append(f'citation {row["citation_id"]} does not resolve: {exc}')
            continue
        if not resolved['key'].startswith(doc_store.STORE_PREFIX + '/'):
            problems.append(
                f'citation {row["citation_id"]} resolves outside the store')
        if resolved['page'] != row['page']:
            problems.append(
                f'citation {row["citation_id"]} resolves to the wrong page')
        if row['quote_located'] and not resolved['quote_located']:
            problems.append(
                f'citation {row["citation_id"]} loses its located quote on '
                'resolution')
    return problems


def check_subject_reachability(manifest):
    """Each subject a document declares must be reachable by its own key."""
    problems = []
    for row in manifest['documents']:
        for subject in row['subjects']:
            if not doc_store.is_mine_subject(subject['mine_id']):
                continue
            citations = doc_store.citations_for_subject(
                manifest, subject['state'], subject['mine_id'])
            if not citations and subject['mine_id'] == row['mine_id']:
                problems.append(
                    f'document {row["doc_id"][:12]} is filed under mine '
                    f'{subject["mine_id"]} but carries no citation for it')
    return problems


def check_viewer(manifest):
    """The shipped viewer must open stored objects, never the portal URL."""
    problems = []
    if not os.path.isfile(VIEWER):
        problems.append('site/viewer.html is missing; citation chips have '
                        'nothing to open')
        return problems
    text = _read_text(VIEWER, 'site/viewer.html')
    for needed in ('assets/pdfjs/pdf.min.mjs', 'assets/pdfjs/pdf.worker.min.mjs'):
        if needed not in text:
            problems.append(f'site/viewer.html does not load {needed}')
    for row in manifest['documents'][:1]:
        if row['source_url'] in text:
            problems.append('site/viewer.html hardcodes a portal URL')
    return problems


def validate(manifest_path, *, store_dir=None, template_path=TEMPLATE):
    manifest = doc_store.load_manifest(manifest_path)
    template_text = _read_text(template_path, 'infra/template.yaml')
    problems = []
    problems.extend(check_private_delivery(template_text))
    problems.extend(check_lifecycle(template_text, manifest['store']))
    problems.extend(check_portal_independence(manifest))
    problems.extend(check_subject_reachability(manifest))
    problems.extend(check_viewer(manifest))
    store_checked = False
    if store_dir is not None:
        store_checked = True
        problems.extend(doc_store.verify_store(manifest, store_dir))
    if problems:
        raise DocStoreGateError('; '.join(problems))
    return {
        'ok': True,
        'manifest': os.path.relpath(manifest_path, ROOT),
        'documents': manifest['metrics']['documents'],
        'pages': manifest['metrics']['pages'],
        'citations': manifest['metrics']['citations'],
        'citations_quote_located': manifest['metrics']['citations_quote_located'],
        'subjects': manifest['metrics']['subjects'],
        'store_objects_hash_verified': store_checked,
        'delivery': manifest['store']['delivery'],
        'effect': 'validation_only_no_upload_or_release_mutation',
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        '--manifest', default=os.path.join(ROOT, doc_store.MANIFEST_RELATIVE),
        help='WS12 document manifest to validate')
    parser.add_argument(
        '--store-dir', default=None,
        help='local store generation to hash-verify (default: skip)')
    parser.add_argument(
        '--require-store', action='store_true',
        help=f'hash-verify the default local store at {DEFAULT_STORE}')
    parser.add_argument(
        '--template', default=TEMPLATE,
        help='CloudFormation template whose delivery contract must agree')
    args = parser.parse_args(argv)
    store_dir = args.store_dir
    if args.require_store and store_dir is None:
        store_dir = DEFAULT_STORE
    try:
        result = validate(
            args.manifest, store_dir=store_dir, template_path=args.template)
    except (OSError, doc_store.DocStoreError, DocStoreGateError) as exc:
        parser.exit(1, f'document store gate failed: {exc}\n')
    print(json.dumps(result, sort_keys=True, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
