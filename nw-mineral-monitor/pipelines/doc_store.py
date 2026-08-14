#!/usr/bin/env python3
"""WS12 document-store contract: key scheme, manifest schema, and resolution.

This module is the shared, dependency-free core of the WS12 document store.
It defines the canonical S3 key scheme, the closed manifest schema, and the
citation resolution used by the browser viewer and the ``open_doc`` tool.  It
holds no PDF, OCR, or AWS code so that the contract can be validated in a
clean checkout with the standard library alone.

Every stored document has exactly two objects: an immutable ``raw`` original
(the provenance copy, byte-identical to what the portal served) and a
``searchable`` copy whose text layer sits on the original pages, so printed
pagination is preserved and a page citation resolves 1:1.  ``doc_id`` is the
SHA-256 of the raw bytes and never a filename, portal URL, or row index, so a
citation survives re-OCR, a re-crawl, and a portal redesign.

The key path records where a document is filed, not the limit of what it
covers.  A bulletin filed under one subject is still cited for every mine in
``subjects``.  This module does not download, hash, OCR, upload, presign, or
mutate any release flag.
"""
from __future__ import annotations

import hashlib
import json
import os
import re


DATASET = 'ws12-document-store'
SCHEMA_VERSION = 1
# Root-relative operator artifact. The repository and site are public, so the
# complete manifest (object keys + citation excerpts) lives only below the
# ignored private runtime tree. Public clients receive a minimized catalog.
MANIFEST_RELATIVE = os.path.join('var', 'ws12', 'document-store-manifest.json')
STORE_PREFIX = 'docs'
KEY_TEMPLATE = 'docs/{state}/{portal}/{mine_id}/{sha256}/{variant}.pdf'
VARIANTS = ('raw', 'searchable')
TEXT_LAYER_STATUSES = ('native', 'ocr_added', 'absent')
# Mine identifiers are not one namespace: WS9 grade evidence uses
# ``nv-grizzly-bear-shaft``, the state surveys use their own catalogue ids,
# MRDS uses numeric dep_ids, and USMIN has no id at all.  Every key segment
# therefore carries its namespace so two datasets can never collide in one
# path, and so a reader can tell a mine record from a district or state
# filing by looking at the key.
ID_NAMESPACES = (
    'ws9-', 'stategeo-', 'mrds-', 'usmin-', 'mlrs-', 'district-', 'statewide-',
)
NON_MINE_PREFIXES = ('district-', 'statewide-')
# A document covering the whole country is filed under this reserved scope
# instead of an arbitrary state; its per-state subjects still enumerate every
# state it is cited for.
NATIONAL_SCOPE = 'US'
DEFAULT_PRESIGN_TTL_SECONDS = 300
DEFAULT_RAW_TRANSITION_DAYS = 30
DEFAULT_RAW_STORAGE_CLASS = 'STANDARD_IA'

SHA_RE = re.compile(r'[0-9a-f]{64}')
DATE_RE = re.compile(r'\d{4}-\d{2}-\d{2}')
STATE_RE = re.compile(r'[A-Z]{2}')
SLUG_RE = re.compile(r'[a-z0-9][a-z0-9-]{0,95}')
PORTAL_RE = re.compile(r'[a-z0-9][a-z0-9-]{0,63}')
ID_RE = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}')

DOCUMENT_REQUIRED = (
    'doc_id', 'state', 'portal', 'mine_id', 'title', 'authority',
    'source_url', 'catalog_url', 'retrieved', 'pages', 'pagination_preserved',
    'raw', 'searchable', 'text_layer', 'subjects', 'source_ids', 'rights',
)
DOCUMENT_OPTIONAL = ('citation', 'publication_year', 'license_note')
RIGHTS_REQUIRED = ('public_domain', 'paywalled', 'basis')
OBJECT_REQUIRED = ('key', 'sha256', 'bytes')
TEXT_LAYER_REQUIRED = ('status', 'tool', 'pages_with_text', 'characters')
SUBJECT_REQUIRED = ('state', 'mine_id', 'label')
CITATION_REQUIRED = (
    'citation_id', 'doc_id', 'page', 'page_cite', 'quote', 'quote_located',
    'state', 'mine_id', 'dataset',
)
CITATION_OPTIONAL = ('source_id', 'mine_name')
STORE_REQUIRED = (
    'key_template', 'variants', 'public_prefix', 'delivery',
    'presign_ttl_seconds', 'raw_transition_days', 'raw_storage_class',
)
METRIC_KEYS = (
    'citations', 'citations_quote_located', 'documents', 'documents_servable',
    'documents_rights_unresolved', 'pages', 'raw_bytes', 'searchable_bytes',
    'subjects', 'text_layer_absent', 'text_layer_native', 'text_layer_ocr_added',
)
MANIFEST_REQUIRED = (
    'schema_version', 'dataset', 'generated', 'store', 'metrics', 'documents',
    'citations',
)


class DocStoreError(ValueError):
    """A document, citation, or manifest violates the WS12 store contract."""


def _reject_constant(value):
    raise ValueError(f'non-standard JSON number {value}')


def _reject_duplicate_keys(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f'duplicate JSON object key {key!r}')
        value[key] = item
    return value


def strict_json_bytes(raw, label):
    try:
        return json.loads(
            raw.decode('utf-8'), parse_constant=_reject_constant,
            object_pairs_hook=_reject_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise DocStoreError(f'{label} is not strict UTF-8 JSON: {exc}') from exc


def load_strict_json(path, label):
    try:
        with open(path, 'rb') as source:
            raw = source.read()
    except OSError as exc:
        raise DocStoreError(f'cannot read {label}: {exc}') from exc
    return strict_json_bytes(raw, label), raw


def canonical_bytes(value):
    try:
        return json.dumps(
            value, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
            allow_nan=False).encode('utf-8')
    except (TypeError, ValueError) as exc:
        raise DocStoreError(f'cannot encode canonical manifest JSON: {exc}') from exc


def sha256_bytes(raw):
    return hashlib.sha256(raw).hexdigest()


def sha256_file(path):
    digest = hashlib.sha256()
    try:
        with open(path, 'rb') as source:
            for chunk in iter(lambda: source.read(1 << 20), b''):
                digest.update(chunk)
    except OSError as exc:
        raise DocStoreError(f'cannot hash {path}: {exc}') from exc
    return digest.hexdigest()


def _expect_keys(value, required, optional, label):
    if not isinstance(value, dict):
        raise DocStoreError(f'{label} must be an object')
    missing = sorted(set(required) - set(value))
    extra = sorted(set(value) - set(required) - set(optional))
    if missing or extra:
        raise DocStoreError(
            f'{label} keys mismatch: missing={missing}, extra={extra}')


def _text(value, label, minimum=1, maximum=4096):
    if (not isinstance(value, str) or value != value.strip() or
            not minimum <= len(value) <= maximum or '\x00' in value or
            '\r' in value or '\n' in value):
        raise DocStoreError(
            f'{label} must be trimmed single-line text of length '
            f'{minimum}..{maximum}')
    return value


def _quote(value, label, minimum=8, maximum=5000):
    if (not isinstance(value, str) or value != value.strip() or
            not minimum <= len(value) <= maximum or '\x00' in value):
        raise DocStoreError(
            f'{label} must be trimmed text of length {minimum}..{maximum}')
    return value


def _identifier(value, label):
    _text(value, label, maximum=128)
    if ID_RE.fullmatch(value) is None:
        raise DocStoreError(f'{label} must be a stable identifier')
    return value


def _slug(value, label, pattern=SLUG_RE):
    _text(value, label, maximum=96)
    if pattern.fullmatch(value) is None:
        raise DocStoreError(
            f'{label} must be a lowercase key-safe slug matching '
            f'{pattern.pattern}')
    return value


def _mine_id(value, label):
    _slug(value, label)
    if not value.startswith(ID_NAMESPACES):
        raise DocStoreError(
            f'{label} must start with one of {list(ID_NAMESPACES)} so mine '
            'identifiers from different datasets cannot collide')
    return value


def _state(value, label):
    if not isinstance(value, str) or STATE_RE.fullmatch(value) is None:
        raise DocStoreError(f'{label} must be a two-letter state code')
    return value


def _date(value, label):
    if not isinstance(value, str) or DATE_RE.fullmatch(value) is None:
        raise DocStoreError(f'{label} must be YYYY-MM-DD')
    year, month, day = (int(part) for part in value.split('-'))
    if not 1800 <= year <= 2200 or not 1 <= month <= 12 or not 1 <= day <= 31:
        raise DocStoreError(f'{label} is not a calendar date')
    return value


def _https(value, label):
    _text(value, label, maximum=2048)
    if re.fullmatch(r'https://[^\s]+', value) is None:
        raise DocStoreError(f'{label} must be an HTTPS URL')
    return value


def _count(value, label, *, positive=False):
    if (not isinstance(value, int) or isinstance(value, bool) or value < 0 or
            (positive and value == 0)):
        qualifier = 'positive' if positive else 'nonnegative'
        raise DocStoreError(f'{label} must be a {qualifier} integer')
    return value


def _sha(value, label):
    if not isinstance(value, str) or SHA_RE.fullmatch(value) is None:
        raise DocStoreError(f'{label} must be a lowercase SHA-256')
    return value


def _true(value, label):
    if value is not True:
        raise DocStoreError(f'{label} must be the literal boolean true')
    return value


def _boolean(value, label):
    if not isinstance(value, bool):
        raise DocStoreError(f'{label} must be a boolean')
    return value


def slugify(value, label, *, maximum=96):
    """Reduce a free-form identifier to a key-safe lowercase slug."""
    if not isinstance(value, str):
        raise DocStoreError(f'{label} must be text')
    slug = re.sub(r'[^a-z0-9]+', '-', value.strip().lower()).strip('-')
    slug = slug[:maximum].rstrip('-')
    if SLUG_RE.fullmatch(slug) is None:
        raise DocStoreError(f'{label} does not reduce to a key-safe slug')
    return slug


def is_mine_subject(mine_id):
    """True when the key segment names a mine rather than a district or state."""
    return not str(mine_id).startswith(NON_MINE_PREFIXES)


def object_key(state, portal, mine_id, doc_id, variant):
    """Derive the canonical S3 key for one stored object.

    Both variants of a document live under the same ``doc_id`` directory —
    the SHA-256 of the raw original — so re-OCR replaces one object without
    moving the document or invalidating a citation.  The key is always
    recomputed from validated parts; a manifest row's asserted key is compared
    against this, never trusted in its place.
    """
    _state(state, 'object_key.state')
    _slug(portal, 'object_key.portal', PORTAL_RE)
    _mine_id(mine_id, 'object_key.mine_id')
    _sha(doc_id, 'object_key.doc_id')
    if variant not in VARIANTS:
        raise DocStoreError(f'object_key.variant must be one of {list(VARIANTS)}')
    return KEY_TEMPLATE.format(
        state=state, portal=portal, mine_id=mine_id, sha256=doc_id,
        variant=variant)


def store_path(store_root, key):
    """Resolve a store key to a local path, refusing traversal outside root."""
    if not isinstance(key, str) or not key.startswith(STORE_PREFIX + '/'):
        raise DocStoreError(f'store key {key!r} must start with {STORE_PREFIX}/')
    parts = key.split('/')
    if any(part in ('', '.', '..') for part in parts):
        raise DocStoreError(f'store key {key!r} has an unsafe path segment')
    root = os.path.realpath(store_root)
    resolved = os.path.realpath(os.path.join(root, *parts))
    if os.path.commonpath([root, resolved]) != root:
        raise DocStoreError(f'store key {key!r} escapes the store root')
    return resolved


def _validate_object(value, label, *, state, portal, mine_id, doc_id, variant):
    _expect_keys(value, OBJECT_REQUIRED, (), label)
    digest = _sha(value['sha256'], f'{label}.sha256')
    size = _count(value['bytes'], f'{label}.bytes', positive=True)
    expected = object_key(state, portal, mine_id, doc_id, variant)
    if value['key'] != expected:
        raise DocStoreError(
            f'{label}.key must be the derived store key {expected!r}')
    return {'key': expected, 'sha256': digest, 'bytes': size}


def _validate_text_layer(value, label, pages):
    _expect_keys(value, TEXT_LAYER_REQUIRED, (), label)
    status = value['status']
    if status not in TEXT_LAYER_STATUSES:
        raise DocStoreError(
            f'{label}.status must be one of {list(TEXT_LAYER_STATUSES)}')
    _text(value['tool'], f'{label}.tool', maximum=200)
    with_text = _count(value['pages_with_text'], f'{label}.pages_with_text')
    characters = _count(value['characters'], f'{label}.characters')
    if with_text > pages:
        raise DocStoreError(
            f'{label}.pages_with_text exceeds the document page count')
    if status == 'absent' and (with_text or characters):
        raise DocStoreError(
            f'{label} reports an absent text layer with extracted text')
    if status != 'absent' and not with_text:
        raise DocStoreError(
            f'{label} claims a text layer but no page carries text')
    return value


def _validate_rights(value, label):
    """Every row states its rights basis explicitly, never by inference.

    ``public_domain`` is recorded as the reviewer stated it, not guessed from
    the publisher: a federal work carries an affirmative basis, and a state
    survey's own publication is recorded as unresolved until someone reviews
    it. Serving is gated on this field, so an unresolved row stays in the
    manifest as a known gap instead of quietly becoming a served document.
    """
    _expect_keys(value, RIGHTS_REQUIRED, (), label)
    public_domain = _boolean(value['public_domain'], f'{label}.public_domain')
    if value['paywalled'] is not False:
        raise DocStoreError(
            f'{label}.paywalled must be false; a paywalled document is not stored')
    return {
        'public_domain': public_domain,
        'paywalled': False,
        'basis': _text(value['basis'], f'{label}.basis', 8, 500),
    }


def servable_documents(manifest):
    """Documents cleared to be uploaded and served, newest contract first."""
    return [row for row in manifest['documents'] if row['rights']['public_domain']]


def withheld_documents(manifest):
    """Documents kept in the manifest but withheld from delivery."""
    return [row for row in manifest['documents'] if not row['rights']['public_domain']]


def _validate_subject(value, label):
    _expect_keys(value, SUBJECT_REQUIRED, (), label)
    return {
        'state': _state(value['state'], f'{label}.state'),
        'mine_id': _mine_id(value['mine_id'], f'{label}.mine_id'),
        'label': _text(value['label'], f'{label}.label', 2, 300),
    }


def validate_document(value, label):
    """Validate one manifest document row and return its normalized form."""
    _expect_keys(value, DOCUMENT_REQUIRED, DOCUMENT_OPTIONAL, label)
    state = _state(value['state'], f'{label}.state')
    portal = _slug(value['portal'], f'{label}.portal', PORTAL_RE)
    mine_id = _mine_id(value['mine_id'], f'{label}.mine_id')
    doc_id = _sha(value['doc_id'], f'{label}.doc_id')
    pages = _count(value['pages'], f'{label}.pages', positive=True)
    _true(value['pagination_preserved'], f'{label}.pagination_preserved')
    raw = _validate_object(
        value['raw'], f'{label}.raw', state=state, portal=portal,
        mine_id=mine_id, doc_id=doc_id, variant='raw')
    if raw['sha256'] != doc_id:
        raise DocStoreError(
            f'{label}.doc_id must be the SHA-256 of the raw original')
    searchable = _validate_object(
        value['searchable'], f'{label}.searchable', state=state, portal=portal,
        mine_id=mine_id, doc_id=doc_id, variant='searchable')
    text_layer = _validate_text_layer(
        value['text_layer'], f'{label}.text_layer', pages)
    if text_layer['status'] == 'ocr_added' and searchable['sha256'] == doc_id:
        raise DocStoreError(
            f'{label} claims an added text layer but the searchable copy is '
            'byte-identical to the raw original')
    if text_layer['status'] != 'ocr_added' and searchable['sha256'] != doc_id:
        raise DocStoreError(
            f'{label} did not add a text layer, so the searchable copy must '
            'be the original bytes')
    subjects = value['subjects']
    if not isinstance(subjects, list) or not subjects:
        raise DocStoreError(f'{label}.subjects must be a non-empty list')
    rows, seen = [], set()
    for index, item in enumerate(subjects):
        subject = _validate_subject(item, f'{label}.subjects[{index}]')
        identity = (subject['state'], subject['mine_id'])
        if identity in seen:
            raise DocStoreError(f'{label}.subjects repeats {identity}')
        seen.add(identity)
        rows.append(subject)
    if (state, mine_id) not in seen:
        raise DocStoreError(
            f'{label}.subjects must include the filing subject '
            f'{(state, mine_id)}')
    source_ids = value['source_ids']
    if not isinstance(source_ids, list) or not source_ids:
        raise DocStoreError(f'{label}.source_ids must be a non-empty list')
    identifiers = []
    for index, item in enumerate(source_ids):
        identifiers.append(_identifier(item, f'{label}.source_ids[{index}]'))
    if len(set(identifiers)) != len(identifiers):
        raise DocStoreError(f'{label}.source_ids has duplicates')
    normalized = {
        'doc_id': doc_id,
        'state': state,
        'portal': portal,
        'mine_id': mine_id,
        'title': _text(value['title'], f'{label}.title', 3, 500),
        'authority': _text(value['authority'], f'{label}.authority', 2, 300),
        'source_url': _https(value['source_url'], f'{label}.source_url'),
        'catalog_url': _https(value['catalog_url'], f'{label}.catalog_url'),
        'retrieved': _date(value['retrieved'], f'{label}.retrieved'),
        'pages': pages,
        'pagination_preserved': True,
        'raw': raw,
        'searchable': searchable,
        'text_layer': text_layer,
        'subjects': sorted(rows, key=lambda row: (row['state'], row['mine_id'])),
        'source_ids': sorted(identifiers),
        'rights': _validate_rights(value['rights'], f'{label}.rights'),
    }
    if 'citation' in value:
        normalized['citation'] = _text(value['citation'], f'{label}.citation', 8, 1000)
    if 'publication_year' in value:
        year = value['publication_year']
        if (not isinstance(year, int) or isinstance(year, bool) or
                not 1800 <= year <= 2100):
            raise DocStoreError(f'{label}.publication_year must be 1800..2100')
        normalized['publication_year'] = year
    if 'license_note' in value:
        normalized['license_note'] = _text(
            value['license_note'], f'{label}.license_note', 8, 1000)
    return normalized


def validate_citation(value, label, documents):
    """Validate one citation row against the documents it must resolve into."""
    _expect_keys(value, CITATION_REQUIRED, CITATION_OPTIONAL, label)
    doc_id = _sha(value['doc_id'], f'{label}.doc_id')
    document = documents.get(doc_id)
    if document is None:
        raise DocStoreError(f'{label}.doc_id references an unknown document')
    page = _count(value['page'], f'{label}.page', positive=True)
    if page > document['pages']:
        raise DocStoreError(
            f'{label}.page {page} is past the end of the cited document')
    page_cite = _text(value['page_cite'], f'{label}.page_cite', 2, 200)
    if not any(character.isdigit() for character in page_cite):
        raise DocStoreError(f'{label}.page_cite must carry a page number')
    normalized = {
        'citation_id': _identifier(value['citation_id'], f'{label}.citation_id'),
        'doc_id': doc_id,
        'page': page,
        'page_cite': page_cite,
        'quote': _quote(value['quote'], f'{label}.quote'),
        'quote_located': _boolean(value['quote_located'], f'{label}.quote_located'),
        'state': _state(value['state'], f'{label}.state'),
        'mine_id': _mine_id(value['mine_id'], f'{label}.mine_id'),
        'dataset': _identifier(value['dataset'], f'{label}.dataset'),
    }
    if normalized['quote_located'] and document['text_layer']['status'] == 'absent':
        raise DocStoreError(
            f'{label} claims a located quote in a document with no text layer')
    if 'source_id' in value:
        normalized['source_id'] = _identifier(value['source_id'], f'{label}.source_id')
    if 'mine_name' in value:
        normalized['mine_name'] = _text(value['mine_name'], f'{label}.mine_name', 2, 300)
    return normalized


def _validate_store(value, label):
    _expect_keys(value, STORE_REQUIRED, (), label)
    if value['key_template'] != KEY_TEMPLATE:
        raise DocStoreError(f'{label}.key_template must be {KEY_TEMPLATE!r}')
    if value['variants'] != list(VARIANTS):
        raise DocStoreError(f'{label}.variants must be {list(VARIANTS)}')
    # The corpus is served only through short-lived presigned GETs.  A true
    # here would mean the bucket policy exposes it to CloudFront crawling.
    if value['public_prefix'] is not False:
        raise DocStoreError(f'{label}.public_prefix must be false')
    if value['delivery'] != 'presigned_get':
        raise DocStoreError(f'{label}.delivery must be presigned_get')
    ttl = _count(value['presign_ttl_seconds'], f'{label}.presign_ttl_seconds',
                 positive=True)
    if ttl > 3600:
        raise DocStoreError(f'{label}.presign_ttl_seconds must be at most 3600')
    _count(value['raw_transition_days'], f'{label}.raw_transition_days',
           positive=True)
    _text(value['raw_storage_class'], f'{label}.raw_storage_class', 4, 64)
    return dict(value)


def recompute_metrics(documents, citations):
    """Derive every metric from the rows.

    The builder and the validator share this so a declared count can never be
    something only one of them believes.
    """
    subjects = {
        (subject['state'], subject['mine_id'])
        for row in documents for subject in row['subjects']
    }
    metrics = {
        'citations': len(citations),
        'citations_quote_located': sum(
            1 for row in citations if row['quote_located']),
        'documents': len(documents),
        'documents_servable': sum(
            1 for row in documents if row['rights']['public_domain']),
        'documents_rights_unresolved': sum(
            1 for row in documents if not row['rights']['public_domain']),
        'pages': sum(row['pages'] for row in documents),
        'raw_bytes': sum(row['raw']['bytes'] for row in documents),
        'searchable_bytes': sum(row['searchable']['bytes'] for row in documents),
        'subjects': len(subjects),
        'text_layer_absent': sum(
            1 for row in documents if row['text_layer']['status'] == 'absent'),
        'text_layer_native': sum(
            1 for row in documents if row['text_layer']['status'] == 'native'),
        'text_layer_ocr_added': sum(
            1 for row in documents if row['text_layer']['status'] == 'ocr_added'),
    }
    if sorted(metrics) != sorted(METRIC_KEYS):
        raise DocStoreError('recomputed metrics do not match the metric keys')
    return metrics


def validate_manifest(document, label='document manifest'):
    """Validate the whole WS12 manifest and return its normalized form.

    Metrics are always recomputed from the rows; an asserted count that does
    not reconcile is a failure rather than a value the caller may rely on.
    """
    _expect_keys(document, MANIFEST_REQUIRED, (), label)
    if document['schema_version'] != SCHEMA_VERSION:
        raise DocStoreError(f'{label}.schema_version must be {SCHEMA_VERSION}')
    if document['dataset'] != DATASET:
        raise DocStoreError(f'{label}.dataset must be {DATASET!r}')
    _date(document['generated'], f'{label}.generated')
    store = _validate_store(document['store'], f'{label}.store')
    rows = document['documents']
    if not isinstance(rows, list) or not rows:
        raise DocStoreError(f'{label}.documents must be a non-empty list')
    documents, keys = {}, {}
    for index, value in enumerate(rows):
        row = validate_document(value, f'{label}.documents[{index}]')
        if row['doc_id'] in documents:
            raise DocStoreError(
                f'{label} has duplicate doc_id {row["doc_id"]}')
        documents[row['doc_id']] = row
        for variant in VARIANTS:
            key = row[variant]['key']
            owner = keys.get(key)
            if owner not in (None, row['doc_id']):
                raise DocStoreError(
                    f'{label} maps store key {key} to two documents')
            keys[key] = row['doc_id']
    citation_rows = document['citations']
    if not isinstance(citation_rows, list):
        raise DocStoreError(f'{label}.citations must be a list')
    citations, citation_ids = [], set()
    for index, value in enumerate(citation_rows):
        row = validate_citation(value, f'{label}.citations[{index}]', documents)
        if row['citation_id'] in citation_ids:
            raise DocStoreError(
                f'{label} has duplicate citation_id {row["citation_id"]}')
        citation_ids.add(row['citation_id'])
        citations.append(row)
    subjects = {
        (subject['state'], subject['mine_id'])
        for row in documents.values() for subject in row['subjects']
    }
    for row in citations:
        if (row['state'], row['mine_id']) not in subjects:
            raise DocStoreError(
                f'citation {row["citation_id"]} cites a subject that no '
                'document declares')
    metrics = recompute_metrics(list(documents.values()), citations)
    if document['metrics'] != metrics:
        raise DocStoreError(
            f'{label}.metrics do not reconcile with the rows: '
            f'declared={document["metrics"]}, computed={metrics}')
    return {
        'schema_version': SCHEMA_VERSION,
        'dataset': DATASET,
        'generated': document['generated'],
        'store': store,
        'metrics': metrics,
        'documents': [documents[key] for key in sorted(documents)],
        'citations': sorted(citations, key=lambda row: row['citation_id']),
    }


def load_manifest(path, label='document manifest'):
    document, raw = load_strict_json(path, label)
    manifest = validate_manifest(document, label)
    if canonical_bytes(manifest) != raw:
        raise DocStoreError(
            f'{label} is not the canonical encoding of its own contents')
    return manifest


def index_documents(manifest):
    return {row['doc_id']: row for row in manifest['documents']}


def index_source_ids(manifest):
    """Map every declared source_id to its document, refusing collisions."""
    index = {}
    for row in manifest['documents']:
        for source_id in row['source_ids']:
            owner = index.get(source_id)
            if owner not in (None, row['doc_id']):
                raise DocStoreError(
                    f'source_id {source_id} maps to two documents')
            index[source_id] = row['doc_id']
    return index


def resolve_doc_id(manifest, doc_id):
    """Resolve a full or unambiguous partial doc_id, or a source_id.

    A partial digest is accepted from 8 hex characters up so a model or a
    hand-typed reference stays usable, but an ambiguous prefix is an error
    rather than an arbitrary pick.
    """
    if not isinstance(doc_id, str):
        raise DocStoreError('doc_id must be text')
    candidate = doc_id.strip().lower()
    documents = index_documents(manifest)
    if candidate in documents:
        return candidate
    sources = index_source_ids(manifest)
    if doc_id.strip() in sources:
        return sources[doc_id.strip()]
    if len(candidate) >= 8 and re.fullmatch(r'[0-9a-f]+', candidate):
        matches = sorted(key for key in documents if key.startswith(candidate))
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise DocStoreError(
                f'doc_id prefix {doc_id!r} matches {len(matches)} documents')
    raise DocStoreError(f'no stored document matches {doc_id!r}')


def resolve_open_doc(manifest, doc_id, page=None, quote=None):  # noqa: C901
    """Resolve an ``open_doc`` request into the object the viewer must open.

    The returned row always names the searchable copy: page citations resolve
    against the copy whose text layer sits on the original pages.  The portal
    URL rides along for provenance and is never what gets opened.
    """
    resolved = resolve_doc_id(manifest, doc_id)
    document = index_documents(manifest)[resolved]
    if not document['rights']['public_domain']:
        raise DocStoreError(
            f'{resolved[:12]} is held pending a public-domain review and is not '
            'served; cite the publisher\'s own record instead')
    citation = None
    if quote:
        wanted = ' '.join(str(quote).split()).lower()
        for row in manifest['citations']:
            if row['doc_id'] != resolved:
                continue
            if ' '.join(row['quote'].split()).lower() == wanted:
                citation = row
                break
    if page is None:
        page = citation['page'] if citation else 1
    page = _count(page, 'open_doc.page', positive=True)
    if page > document['pages']:
        raise DocStoreError(
            f'page {page} is past the end of a {document["pages"]}-page document')
    return {
        'doc_id': resolved,
        'title': document['title'],
        'authority': document['authority'],
        'state': document['state'],
        'portal': document['portal'],
        'mine_id': document['mine_id'],
        'page': page,
        'pages': document['pages'],
        'quote': citation['quote'] if citation else (quote or None),
        'quote_located': bool(citation and citation['quote_located']),
        'citation_id': citation['citation_id'] if citation else None,
        'page_cite': citation['page_cite'] if citation else f'p. {page}',
        'key': document['searchable']['key'],
        'raw_key': document['raw']['key'],
        'sha256': document['searchable']['sha256'],
        'raw_sha256': document['raw']['sha256'],
        'bytes': document['searchable']['bytes'],
        'raw_bytes': document['raw']['bytes'],
        'text_layer': document['text_layer']['status'],
        'source_url': document['source_url'],
        'catalog_url': document['catalog_url'],
        'retrieved': document['retrieved'],
    }


def citations_for_subject(manifest, state, mine_id):
    """Every citation filed against one subject, newest page order preserved."""
    return [
        row for row in manifest['citations']
        if row['state'] == state and row['mine_id'] == mine_id
    ]


def verify_store(manifest, store_root):
    """Hash-verify every raw and searchable object named by the manifest.

    Local presence is never inferred from the manifest: a missing object and a
    drifted object are both reported, and an empty report is the only pass.
    """
    problems = []
    for row in manifest['documents']:
        for variant in VARIANTS:
            entry = row[variant]
            path = store_path(store_root, entry['key'])
            if not os.path.isfile(path):
                problems.append(f'{entry["key"]} is missing from the store')
                continue
            size = os.path.getsize(path)
            if size != entry['bytes']:
                problems.append(
                    f'{entry["key"]} is {size} bytes, manifest says '
                    f'{entry["bytes"]}')
                continue
            digest = sha256_file(path)
            if digest != entry['sha256']:
                problems.append(
                    f'{entry["key"]} hashes to {digest}, manifest says '
                    f'{entry["sha256"]}')
    return problems
