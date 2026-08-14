#!/usr/bin/env python3
"""Build the WS12 document store: raw originals, searchable copies, manifest.

The builder is a local ingester, not a crawler.  It consumes documents that
the checksum-pinned WS11 grade-source inventories (or an explicitly reviewed
manual entry) already identify by URL, byte count, SHA-256, and page count.
Every file is re-hashed before it is admitted; a byte of drift fails the build
rather than quietly re-publishing.

For each admitted document it writes two store objects.  ``raw.pdf`` is the
original, unmodified.  ``searchable.pdf`` carries a text layer over the
original pages so page N of the citation is page N of the object; when the
original already has its own text layer the searchable copy is deliberately
the same bytes, and the manifest says so rather than pretending an OCR pass
happened.  Citations are then located inside that text layer, so a quote the
viewer cannot highlight is recorded as unlocated instead of asserted.

Store objects are hardlinked to their sources where the bytes are identical,
so a generation costs almost no disk.  The builder does not upload to S3,
presign anything, edit ``states/*.yaml``, touch release flags, or change any
other browser artifact. Its complete manifest is written only to the ignored
private runtime tree at ``var/ws12/document-store-manifest.json``.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import date

import doc_store
import document_assets
from doc_store import DocStoreError


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
SITE = os.path.join(ROOT, 'site')
CONFIG = os.path.join(HERE, 'config')
REGISTRY = os.path.join(CONFIG, 'ws12_documents.json')
CITATIONS = os.path.join(CONFIG, 'ws12_citations.json')
RESEARCH = os.path.join(ROOT, 'grades-research')
LEGACY_GRADES = os.path.join(SITE, 'data', 'grades', 'grades.json')
DEFAULT_STORE = os.path.join(HERE, 'cache', 'ws12', 'store')
DEFAULT_HARVEST_MANIFEST = os.path.join(ROOT, 'var', 'ws12', 'manifest.jsonl')
DEFAULT_ASSET_CATALOG = os.path.join(ROOT, 'var', 'ws12', 'document-assets.json')
REGISTRY_DATASET = 'ws12-document-registry'
OCR_LANGUAGE = 'eng'
OCR_DPI = 300
# A quote shorter than this is too weak to prove a page match on its own.
MIN_QUOTE_ANCHOR = 24

REGISTRY_REQUIRED = ('schema_version', 'dataset', 'portals', 'documents')
ENTRY_REQUIRED = ('source_id', 'state', 'portal', 'mine_id', 'retrieved',
                  'public_domain', 'rights_basis')
ENTRY_OPTIONAL = (
    'inventory', 'filing', 'title', 'authority', 'citation', 'publication_year',
    'catalog_url', 'document_url', 'local_path', 'bytes', 'sha256', 'pages',
    'license_note', 'subjects',
)
INVENTORY_KEYS = (
    'source_id', 'title', 'authority', 'citation', 'publication_year',
    'catalog_url', 'document_url', 'local_path', 'bytes', 'sha256', 'pages',
    'text_mode',
)
PAGE_CITE_RE = re.compile(r'p{1,2}\.\s*(\d+)')


class DocumentBuildError(ValueError):
    """An input document or citation violates the WS12 ingest contract."""


def _fail(message):
    raise DocumentBuildError(message)


def _load(path, label):
    document, _ = doc_store.load_strict_json(path, label)
    return document


def _normalize(text):
    return ' '.join(str(text).split()).lower()


def _inside(path, root):
    resolved = os.path.realpath(path)
    base = os.path.realpath(root)
    return os.path.commonpath([base, resolved]) == base


# ---------------------------------------------------------------------------
# PDF boundary.  These two functions are the only place the builder touches a
# PDF toolchain; tests replace them so the contract is exercised without
# PyMuPDF, Tesseract, or a private PDF cache installed.
# ---------------------------------------------------------------------------

def _probe_pdf(path):
    """Return page count and per-page text for one PDF."""
    try:
        import fitz
    except ImportError as exc:  # pragma: no cover - operator toolchain
        _fail('PyMuPDF (fitz) is required to ingest documents: '
              f'{exc}. Install it or run with --manifest-only.')
    with fitz.open(path) as document:
        pages = [page.get_text() or '' for page in document]
        geometry = [{
            'media_box': [round(float(value), 6) for value in page.mediabox],
            'crop_box': [round(float(value), 6) for value in page.cropbox],
            'rotation': int(page.rotation),
        } for page in document]
    return {
        'pages': len(pages),
        'page_texts': pages,
        'page_geometry': geometry,
        'pages_with_text': sum(1 for text in pages if text.strip()),
        'characters': sum(len(text.strip()) for text in pages),
    }


def _ocr_pdf(source_path, target_path):
    """Overlay a text layer on the ORIGINAL pages and report the tool used.

    ``ocrmypdf`` is preferred because it is the reference implementation of
    exactly this operation.  When it is not installed the builder falls back
    to rendering each page, running Tesseract, and inserting invisible text at
    the recognised word boxes of the untouched original page.  Both paths add
    a layer; neither rewrites, reflows, or re-rasterizes the page content, so
    pagination is preserved either way.
    """
    if shutil.which('ocrmypdf'):
        command = [
            'ocrmypdf', '--skip-text', '--output-type', 'pdf', '--quiet',
            '--language', OCR_LANGUAGE, source_path, target_path,
        ]
        try:
            subprocess.run(command, check=True, capture_output=True)
        except (OSError, subprocess.CalledProcessError) as exc:
            _fail(f'ocrmypdf failed on {source_path}: {exc}')
        return f'ocrmypdf {_tool_version("ocrmypdf")}'
    return _ocr_pdf_with_tesseract(source_path, target_path)


def _tool_version(name):
    try:
        completed = subprocess.run(
            [name, '--version'], check=True, capture_output=True, text=True)
    except (OSError, subprocess.CalledProcessError):
        return 'unknown-version'
    output = (completed.stdout or completed.stderr or '').strip().splitlines()
    return output[0].strip() if output else 'unknown-version'


def _ocr_pdf_with_tesseract(source_path, target_path):
    try:
        import fitz
    except ImportError as exc:  # pragma: no cover - operator toolchain
        _fail(f'PyMuPDF (fitz) is required for the OCR fallback: {exc}')
    if not shutil.which('tesseract'):
        _fail('neither ocrmypdf nor tesseract is installed; a scanned '
              'document cannot be made searchable')
    version = _tool_version('tesseract')
    with fitz.open(source_path) as document:
        for index in range(document.page_count):
            page = document[index]
            if (page.get_text().strip()):
                continue
            words = _tesseract_words(page, fitz)
            if not words:
                continue
            _insert_invisible_text(page, words, fitz)
        document.save(target_path, garbage=0, deflate=True)
    return f'pymupdf+tesseract {version}'


def _tesseract_words(page, fitz):
    """Recognise words on one rendered page and map them back to page points."""
    zoom = OCR_DPI / 72.0
    pixmap = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), colorspace=fitz.csGRAY)
    with tempfile.TemporaryDirectory() as directory:
        image_path = os.path.join(directory, 'page.png')
        pixmap.save(image_path)
        try:
            completed = subprocess.run(
                ['tesseract', image_path, 'stdout', '-l', OCR_LANGUAGE, 'tsv'],
                check=True, capture_output=True, text=True)
        except (OSError, subprocess.CalledProcessError) as exc:
            _fail(f'tesseract failed on a rendered page: {exc}')
    words = []
    for line in completed.stdout.splitlines()[1:]:
        parts = line.split('\t')
        if len(parts) < 12:
            continue
        text = parts[11].strip()
        if not text:
            continue
        try:
            confidence = float(parts[10])
            left, top, width, height = (int(parts[index]) for index in range(6, 10))
        except ValueError:
            continue
        if confidence < 0:
            continue
        words.append({
            'text': text,
            'rect': fitz.Rect(left / zoom, top / zoom,
                              (left + width) / zoom, (top + height) / zoom),
        })
    return words


def _insert_invisible_text(page, words, fitz):
    """Write render-mode-3 text at each word box, leaving the page art alone."""
    for word in words:
        rect = word['rect']
        size = max(1.0, rect.height * 0.85)
        try:
            page.insert_text(
                fitz.Point(rect.x0, rect.y1 - rect.height * 0.15), word['text'],
                fontsize=size, fontname='helv', render_mode=3, overlay=True)
        except (ValueError, RuntimeError):
            continue


# ---------------------------------------------------------------------------
# Registry and inventory
# ---------------------------------------------------------------------------

def load_registry(path=REGISTRY):
    document = _load(path, 'ws12 document registry')
    doc_store._expect_keys(document, REGISTRY_REQUIRED, (), 'registry')
    if document['dataset'] != REGISTRY_DATASET:
        _fail(f'registry.dataset must be {REGISTRY_DATASET!r}')
    if document['schema_version'] != 1:
        _fail('registry.schema_version must be 1')
    portals = document['portals']
    if not isinstance(portals, dict) or not portals:
        _fail('registry.portals must be a non-empty host map')
    for host, slug in portals.items():
        doc_store._text(host, f'registry.portals[{host}].host', 3, 253)
        doc_store._slug(slug, f'registry.portals[{host}]', doc_store.PORTAL_RE)
    entries = document['documents']
    if not isinstance(entries, list) or not entries:
        _fail('registry.documents must be a non-empty list')
    rows = []
    for index, entry in enumerate(entries):
        label = f'registry.documents[{index}]'
        doc_store._expect_keys(entry, ENTRY_REQUIRED, ENTRY_OPTIONAL, label)
        doc_store._identifier(entry['source_id'], f'{label}.source_id')
        doc_store._state(entry['state'], f'{label}.state')
        doc_store._slug(entry['portal'], f'{label}.portal', doc_store.PORTAL_RE)
        doc_store._mine_id(entry['mine_id'], f'{label}.mine_id')
        doc_store._date(entry['retrieved'], f'{label}.retrieved')
        doc_store._boolean(entry['public_domain'], f'{label}.public_domain')
        doc_store._text(entry['rights_basis'], f'{label}.rights_basis', 8, 500)
        if entry['portal'] not in set(portals.values()):
            _fail(f'{label}.portal is not declared in registry.portals')
        rows.append(entry)
    return {'portals': portals, 'documents': rows}


def _inventory_source(entry, label, cache):
    """Resolve a registry row to its checksum-pinned document identity."""
    if 'inventory' in entry:
        relative = entry['inventory']
        if os.path.isabs(relative) or '..' in relative.split(os.sep):
            _fail(f'{label}.inventory must be a repository-relative path')
        path = os.path.join(ROOT, relative)
        if path not in cache:
            document = _load(path, relative)
            doc_store._expect_keys(
                document, ('schema_version', 'dataset', 'sources'), (), relative)
            cache[path] = {
                row['source_id']: row for row in document['sources']
                if isinstance(row, dict) and 'source_id' in row
            }
        source = cache[path].get(entry['source_id'])
        if source is None:
            _fail(f'{label}.source_id is not in {relative}')
        missing = sorted(set(INVENTORY_KEYS) - set(source))
        if missing:
            _fail(f'{relative} source {entry["source_id"]} is missing {missing}')
        return dict(source)
    required = ('title', 'authority', 'catalog_url', 'document_url',
                'local_path', 'bytes', 'sha256', 'pages')
    missing = sorted(name for name in required if name not in entry)
    if missing:
        _fail(f'{label} has no inventory and is missing {missing}')
    return {name: entry[name] for name in required if name in entry} | {
        name: entry[name] for name in ('citation', 'publication_year')
        if name in entry
    }


def _verify_local_document(source, label):
    """Re-hash the staged file; drift or absence fails before anything is written."""
    relative = source['local_path']
    if os.path.isabs(relative) or '..' in relative.split(os.sep):
        _fail(f'{label}.local_path must be a repository-relative path')
    path = os.path.join(ROOT, relative)
    if not _inside(path, ROOT) or os.path.islink(path):
        _fail(f'{label}.local_path must resolve to a real file inside the repository')
    if not os.path.isfile(path):
        _fail(f'{label} is not staged locally at {relative}; fetch it first')
    size = os.path.getsize(path)
    if size != source['bytes']:
        _fail(f'{label} is {size} bytes, the inventory pins {source["bytes"]}')
    digest = doc_store.sha256_file(path)
    if digest != source['sha256']:
        _fail(f'{label} hashes to {digest}, the inventory pins {source["sha256"]}')
    with open(path, 'rb') as handle:
        if handle.read(5) != b'%PDF-':
            _fail(f'{label} is not a PDF')
    return path, digest


def _link_or_copy(source_path, target_path):
    """Place store bytes without a second copy when the filesystem allows it."""
    os.makedirs(os.path.dirname(target_path), exist_ok=True)
    if os.path.exists(target_path):
        if doc_store.sha256_file(target_path) == doc_store.sha256_file(source_path):
            return 'present'
        os.unlink(target_path)
    try:
        os.link(source_path, target_path)
        return 'hardlinked'
    except OSError:
        shutil.copyfile(source_path, target_path)
        return 'copied'


# ---------------------------------------------------------------------------
# Citation sources
# ---------------------------------------------------------------------------

def _config_citations(path=None):
    """Read the reviewed WS12 citation set for documents outside WS9 evidence.

    Some stored documents — a survey's own mine-file scan, a republished MILS
    or MRDS record sheet — carry no grade evidence at all.  Their citations
    are reviewed here instead, and are held to the same rule: the quote is
    verbatim from the cited page, and the builder decides whether it can be
    located rather than the reviewer asserting it.
    """
    path = path or CITATIONS
    if not os.path.isfile(path):
        return []
    document = _load(path, os.path.relpath(path, ROOT))
    doc_store._expect_keys(
        document,
        ('schema_version', 'dataset', 'reviewed_on', 'reviewed_by',
         'review_method', 'citations'), (), 'ws12 reviewed citations')
    rows = []
    for index, value in enumerate(document['citations']):
        label = f'ws12 reviewed citations[{index}]'
        doc_store._expect_keys(
            value,
            ('citation_id', 'source_id', 'state', 'mine_id', 'mine_name',
             'page_cite', 'pdf_page', 'quote'), (), label)
        rows.append({
            'citation_id': doc_store._identifier(
                value['citation_id'], f'{label}.citation_id'),
            'source_id': doc_store._identifier(
                value['source_id'], f'{label}.source_id'),
            'state': doc_store._state(value['state'], f'{label}.state'),
            'mine_id': doc_store._mine_id(value['mine_id'], f'{label}.mine_id'),
            'mine_name': value['mine_name'],
            'page_cite': value['page_cite'],
            'declared_page': doc_store._count(
                value['pdf_page'], f'{label}.pdf_page', positive=True),
            'quote': doc_store._quote(value['quote'], f'{label}.quote'),
            'dataset': document['dataset'],
        })
    return rows


def _reviewed_citations(states):
    """Read the reviewed WS11 grade evidence as citation candidates."""
    rows = []
    for code in sorted(states):
        path = os.path.join(RESEARCH, code.lower(), 'reviewed_grade_evidence.json')
        if not os.path.isfile(path):
            continue
        document = _load(path, f'grades-research/{code.lower()}/reviewed_grade_evidence.json')
        dataset = document.get('dataset') or 'ws11-reviewed-grade-extraction'
        for mine in document.get('mines') or []:
            for evidence in mine.get('evidence') or []:
                rows.append({
                    'citation_id': evidence['evidence_id'],
                    'source_id': evidence['source_id'],
                    'state': code,
                    'mine_id': 'ws9-' + mine['mine_id'],
                    'mine_name': mine['name'],
                    'page_cite': evidence['page_cite'],
                    'declared_page': evidence.get('pdf_page'),
                    'quote': evidence['verbatim_quote'],
                    'dataset': dataset,
                })
    return rows


def _legacy_citations(documents_by_url, limit=None):
    """Read the browser grade aggregate's page cites as citation candidates.

    These are the WS8/WS9 rows the map already renders.  A row is only turned
    into a citation when its source URL is a document we actually store; the
    rest keep their existing portal link and gain nothing, which is the honest
    outcome rather than a chip that opens the wrong file.
    """
    if not os.path.isfile(LEGACY_GRADES):
        return []
    document = _load(LEGACY_GRADES, 'site/data/grades/grades.json')
    count = int(document.get('n') or 0)
    rows, seen = [], set()
    for index in range(count):
        url = (document.get('url') or [None] * count)[index]
        quote = (document.get('quote') or [None] * count)[index]
        citation = (document.get('src') or [None] * count)[index]
        state = (document.get('st') or [None] * count)[index]
        if not url or not quote or not citation or not state:
            continue
        entry = documents_by_url.get(url)
        if entry is None:
            continue
        match = PAGE_CITE_RE.search(citation)
        if match is None:
            continue
        identity = (entry['doc_id'], _normalize(quote))
        if identity in seen:
            continue
        seen.add(identity)
        rows.append({
            'citation_id': f'grades-row-{index}',
            'doc_id': entry['doc_id'],
            'state': state,
            'mine_id': entry['mine_id'],
            'mine_name': (document.get('name') or [None] * count)[index],
            'page_cite': f'p. {match.group(1)}',
            'printed_page': int(match.group(1)),
            'quote': quote,
            'dataset': 'ws9-browser-grade-aggregate',
        })
        if limit is not None and len(rows) >= limit:
            break
    return rows


def _citation_row(candidate, doc_id, page, located, source_id=None):
    row = {
        'citation_id': candidate['citation_id'],
        'doc_id': doc_id,
        'page': page,
        'page_cite': candidate['page_cite'],
        'quote': candidate['quote'],
        'quote_located': located,
        'state': candidate['state'],
        'mine_id': candidate['mine_id'],
        'dataset': candidate['dataset'],
    }
    name = (candidate.get('mine_name') or '').strip()
    if 2 <= len(name) <= 300:
        row['mine_name'] = name
    if source_id:
        row['source_id'] = source_id
    return row


def _locate_quote(page_texts, quote, declared_page=None, printed_page=None):
    """Find the page whose text layer actually carries the quote.

    Preference order is the page the text layer proves, then the page the
    reviewer recorded, then the printed page number.  Only the first of those
    counts as located, because only it can be highlighted.
    """
    wanted = _normalize(quote)
    matches = []
    if len(wanted) >= MIN_QUOTE_ANCHOR:
        for index, text in enumerate(page_texts):
            if wanted in _normalize(text):
                matches.append(index + 1)
        if not matches:
            anchor = ' '.join(wanted.split()[:8])
            if len(anchor) >= MIN_QUOTE_ANCHOR:
                for index, text in enumerate(page_texts):
                    if anchor in _normalize(text):
                        matches.append(index + 1)
    if len(matches) == 1:
        return matches[0], True
    if declared_page and matches and declared_page in matches:
        return declared_page, True
    if matches:
        return matches[0], True
    for candidate in (declared_page, printed_page):
        if candidate and 1 <= candidate <= len(page_texts):
            return candidate, False
    return 1, False


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

def build(*, registry_path=REGISTRY, store_dir=DEFAULT_STORE, manifest_path=None,
          generated=None, legacy_limit=None, probe=None, ocr=None,
          harvest_manifest_path=None, asset_catalog_path=None,
          require_harvest_lineage=None):
    probe = probe or _probe_pdf
    ocr = ocr or _ocr_pdf
    manifest_path = manifest_path or os.path.join(ROOT, doc_store.MANIFEST_RELATIVE)
    registry = load_registry(registry_path)
    production_registry = (
        os.path.realpath(registry_path) == os.path.realpath(REGISTRY))
    explicit_harvest_manifest = harvest_manifest_path is not None
    if require_harvest_lineage is None:
        require_harvest_lineage = production_registry
    if harvest_manifest_path is None and production_registry:
        harvest_manifest_path = os.path.join(
            ROOT, 'var', 'ws12', 'manifest.jsonl')
    if harvest_manifest_path and asset_catalog_path is None:
        asset_catalog_path = os.path.join(
            ROOT, 'var', 'ws12', 'document-assets.json')
    harvest_rows = None
    if harvest_manifest_path:
        if not os.path.isfile(harvest_manifest_path):
            if require_harvest_lineage or explicit_harvest_manifest:
                _fail('the document store requires the canonical harvest '
                      f'manifest at {harvest_manifest_path}')
        else:
            try:
                harvest_rows = document_assets.read_harvest_manifest(
                    harvest_manifest_path)
            except document_assets.DocumentAssetError as exc:
                _fail(f'canonical harvest manifest is invalid: {exc}')
    inventories = {}
    admitted, claimed = {}, {}
    rights_skipped = 0
    for index, entry in enumerate(registry['documents']):
        label = f'registry.documents[{index}] ({entry["source_id"]})'
        if not entry['public_domain']:
            # Public reachability is not a storage license. Keep unresolved
            # rows in the reviewed registry, but do not read, copy, OCR, or
            # publish their bytes.
            rights_skipped += 1
            continue
        source = _inventory_source(entry, label, inventories)
        path, digest = _verify_local_document(source, label)
        filing = (entry['state'], entry['portal'], entry['mine_id'])
        row = admitted.setdefault(digest, {
            'doc_id': digest,
            'path': path,
            'source': source,
            'filings': set(),
            'retrieved': entry['retrieved'],
            'subjects': {},
            'source_ids': set(),
            'license_note': None,
            'rights': None,
        })
        rights = {'public_domain': entry['public_domain'], 'paywalled': False,
                  'basis': entry['rights_basis']}
        if row['rights'] is not None and row['rights'] != rights:
            # Shared bytes cannot be public domain in one state's registry row
            # and unresolved in another's; the disagreement is the finding.
            _fail(f'{label} states different rights for these bytes than an '
                  'earlier row; resolve the disagreement before storing them')
        row['rights'] = rights
        row['filings'].add(filing)
        if entry.get('filing'):
            # One document, one key path.  A shared document (PP 610 sits in
            # seven state inventories) names its filing row explicitly rather
            # than letting registry order decide where it lands.
            previous = claimed.get(digest)
            if previous not in (None, filing):
                _fail(f'{label} is the second row to claim the filing key for '
                      f'these bytes; {previous} already did')
            claimed[digest] = filing
        row['source_ids'].add(entry['source_id'])
        row['retrieved'] = min(row['retrieved'], entry['retrieved'])
        if entry.get('license_note'):
            row['license_note'] = entry['license_note']
        for subject_index, subject in enumerate(entry.get('subjects') or []):
            doc_store._expect_keys(
                subject, doc_store.SUBJECT_REQUIRED, (),
                f'{label}.subjects[{subject_index}]')
            row['subjects'][(subject['state'], subject['mine_id'])] = subject

    filings = {}
    for digest, row in admitted.items():
        if digest in claimed:
            filings[digest] = claimed[digest]
        elif len(row['filings']) == 1:
            filings[digest] = next(iter(row['filings']))
        else:
            _fail(f'{digest[:12]} is registered under {len(row["filings"])} '
                  'different key paths and no row is marked "filing": true')

    states = {state for state, _, _ in filings.values()}
    reviewed = _reviewed_citations(states | {
        subject[0] for row in admitted.values() for subject in row['subjects']
    }) + _config_citations()
    reviewed_by_source = {}
    for citation in reviewed:
        reviewed_by_source.setdefault(citation['source_id'], []).append(citation)

    documents, citations = [], []
    os.makedirs(store_dir, exist_ok=True)
    for digest in sorted(admitted):
        row = admitted[digest]
        state, portal, mine_id = filings[digest]
        source = row['source']
        probed = probe(row['path'])
        if probed['pages'] != source['pages']:
            _fail(f'{source["source_id"] if "source_id" in source else digest[:12]} '
                  f'has {probed["pages"]} pages, the inventory pins {source["pages"]}')
        raw_key = doc_store.object_key(state, portal, mine_id, digest, 'raw')
        searchable_key = doc_store.object_key(
            state, portal, mine_id, digest, 'searchable')
        _link_or_copy(row['path'], doc_store.store_path(store_dir, raw_key))
        searchable_path = doc_store.store_path(store_dir, searchable_key)
        if probed['pages_with_text'] == probed['pages']:
            # The original already carries its own text layer.  Re-OCR would
            # only add noise, so the searchable copy is deliberately the same
            # bytes and the manifest records that rather than implying a pass.
            _link_or_copy(row['path'], searchable_path)
            searchable_digest = digest
            searchable_bytes = source['bytes']
            page_texts = probed['page_texts']
            text_layer = {
                'status': 'native',
                'tool': 'source text layer preserved unmodified',
                'pages_with_text': probed['pages_with_text'],
                'characters': probed['characters'],
            }
        else:
            with tempfile.TemporaryDirectory(dir=store_dir) as staging:
                pending = os.path.join(staging, 'searchable.pdf')
                tool = ocr(row['path'], pending)
                overlaid = probe(pending)
                if overlaid['pages'] != probed['pages']:
                    _fail(f'the searchable copy of {digest[:12]} has '
                          f'{overlaid["pages"]} pages, the original has '
                          f'{probed["pages"]}; pagination must be preserved')
                if overlaid['page_geometry'] != probed['page_geometry']:
                    _fail(f'the searchable copy of {digest[:12]} changed a page '
                          'MediaBox, CropBox, or rotation; original page geometry '
                          'must be preserved')
                if not overlaid['pages_with_text']:
                    _fail(f'the OCR pass on {digest[:12]} produced no readable text')
                searchable_digest = doc_store.sha256_file(pending)
                if searchable_digest == digest:
                    _fail(f'the OCR pass on {digest[:12]} produced the original '
                          'bytes; nothing was added')
                searchable_bytes = os.path.getsize(pending)
                page_texts = overlaid['page_texts']
                os.makedirs(os.path.dirname(searchable_path), exist_ok=True)
                os.replace(pending, searchable_path)
            text_layer = {
                'status': 'ocr_added',
                'tool': tool,
                'pages_with_text': overlaid['pages_with_text'],
                'characters': overlaid['characters'],
            }
        subjects = dict(row['subjects'])
        subjects.setdefault((state, mine_id), {
            'state': state, 'mine_id': mine_id,
            'label': source.get('title') or mine_id,
        })
        document = {
            'doc_id': digest,
            'state': state,
            'portal': portal,
            'mine_id': mine_id,
            'title': source['title'],
            'authority': source['authority'],
            'source_url': source['document_url'],
            'catalog_url': source['catalog_url'],
            'retrieved': row['retrieved'],
            'pages': probed['pages'],
            'pagination_preserved': True,
            'raw': {'key': raw_key, 'sha256': digest, 'bytes': source['bytes']},
            'searchable': {'key': searchable_key, 'sha256': searchable_digest,
                           'bytes': searchable_bytes},
            'text_layer': text_layer,
            'subjects': sorted(subjects.values(),
                               key=lambda item: (item['state'], item['mine_id'])),
            'source_ids': sorted(row['source_ids']),
            'rights': row['rights'],
        }
        if source.get('citation'):
            document['citation'] = source['citation']
        if source.get('publication_year'):
            document['publication_year'] = source['publication_year']
        if row['license_note']:
            document['license_note'] = row['license_note']
        documents.append(document)

        declared = {
            (subject['state'], subject['mine_id'])
            for subject in document['subjects']
        }
        for source_id in document['source_ids']:
            for candidate in reviewed_by_source.get(source_id, []):
                if (candidate['state'], candidate['mine_id']) not in declared:
                    continue
                page, located = _locate_quote(
                    page_texts, candidate['quote'], candidate['declared_page'])
                citations.append(_citation_row(
                    candidate, digest, page, located, source_id=source_id))
        row['page_texts'] = page_texts
        row['document'] = document

    # The browser grade aggregate identifies rows by array index and cites a
    # printed page inside a free-text string.  A row joins the store only when
    # its source URL is a document we hold and the mine it names is already a
    # declared subject of that document; anything else keeps its portal link.
    by_url = {}
    for digest in sorted(admitted):
        document = admitted[digest]['document']
        by_url.setdefault(document['source_url'], document)
    seen_ids = {citation['citation_id'] for citation in citations}
    for candidate in _legacy_citations(by_url, legacy_limit):
        if candidate['citation_id'] in seen_ids:
            continue
        row = admitted[candidate['doc_id']]
        declared = {
            (subject['state'], subject['mine_id'])
            for subject in row['document']['subjects']
        }
        if (candidate['state'], candidate['mine_id']) not in declared:
            continue
        page, located = _locate_quote(
            row['page_texts'], candidate['quote'], None,
            candidate.get('printed_page'))
        citations.append(_citation_row(
            candidate, candidate['doc_id'], page, located))
        seen_ids.add(candidate['citation_id'])

    manifest = {
        'schema_version': doc_store.SCHEMA_VERSION,
        'dataset': doc_store.DATASET,
        'generated': generated or date.today().isoformat(),
        'store': {
            'key_template': doc_store.KEY_TEMPLATE,
            'variants': list(doc_store.VARIANTS),
            'public_prefix': False,
            'delivery': 'presigned_get',
            'presign_ttl_seconds': doc_store.DEFAULT_PRESIGN_TTL_SECONDS,
            'raw_transition_days': doc_store.DEFAULT_RAW_TRANSITION_DAYS,
            'raw_storage_class': doc_store.DEFAULT_RAW_STORAGE_CLASS,
        },
        'metrics': {},
        'documents': documents,
        'citations': citations,
    }
    manifest['metrics'] = _recompute_metrics(manifest)
    normalized = doc_store.validate_manifest(manifest)
    asset_catalog = None
    if harvest_rows is not None:
        try:
            asset_catalog = document_assets.build_catalog(
                harvest_rows, normalized, generated=normalized['generated'])
        except document_assets.DocumentAssetError as exc:
            _fail(f'document asset lineage does not reconcile: {exc}')
    raw = doc_store.canonical_bytes(normalized)
    os.makedirs(os.path.dirname(manifest_path), exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=os.path.dirname(manifest_path))
    try:
        with os.fdopen(handle, 'wb') as target:
            target.write(raw)
            target.flush()
            os.fsync(target.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, manifest_path)
    except BaseException:
        if os.path.exists(temporary):
            os.unlink(temporary)
        raise
    problems = doc_store.verify_store(normalized, store_dir)
    if problems:
        _fail('the freshly written store does not verify: ' + '; '.join(problems))
    lineage = None
    if asset_catalog is not None:
        if not asset_catalog_path:
            _fail('asset_catalog_path is required when harvest lineage is enabled')
        try:
            lineage = document_assets.write_catalog(
                asset_catalog, asset_catalog_path)
        except document_assets.DocumentAssetError as exc:
            _fail(f'cannot write document asset lineage: {exc}')
    result = {
        'manifest': os.path.relpath(manifest_path, ROOT),
        'store_dir': os.path.relpath(store_dir, ROOT),
        'documents': normalized['metrics']['documents'],
        'documents_skipped_rights': rights_skipped,
        'pages': normalized['metrics']['pages'],
        'citations': normalized['metrics']['citations'],
        'citations_quote_located': normalized['metrics']['citations_quote_located'],
        'text_layer_native': normalized['metrics']['text_layer_native'],
        'text_layer_ocr_added': normalized['metrics']['text_layer_ocr_added'],
        'raw_bytes': normalized['metrics']['raw_bytes'],
        'searchable_bytes': normalized['metrics']['searchable_bytes'],
        'sha256': doc_store.sha256_bytes(raw),
        'effect': 'local_store_and_manifest_only_no_upload_or_release_mutation',
    }
    if lineage is not None:
        result['asset_catalog'] = os.path.relpath(asset_catalog_path, ROOT)
        result['asset_catalog_sha256'] = lineage['sha256']
        result['asset_catalog_assets'] = lineage['assets']
    return result


def _recompute_metrics(manifest):
    return doc_store.recompute_metrics(
        manifest['documents'], manifest['citations'])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--registry', default=REGISTRY,
                        help='reviewed WS12 document registry')
    parser.add_argument('--store-dir', default=DEFAULT_STORE,
                        help='local store generation to build')
    parser.add_argument('--manifest', default=None,
                        help=('manifest to write (default '
                              'var/ws12/document-store-manifest.json)'))
    parser.add_argument('--generated', default=None,
                        help='override the generated date (YYYY-MM-DD)')
    parser.add_argument('--legacy-limit', type=int, default=None,
                        help='cap browser-aggregate citations (default: all)')
    parser.add_argument('--harvest-manifest', default=None,
                        help='canonical Part A JSONL (required by production registry)')
    parser.add_argument('--asset-catalog', default=None,
                        help='private normalized lineage output under var/ws12')
    args = parser.parse_args(argv)
    try:
        result = build(
            registry_path=args.registry, store_dir=args.store_dir,
            manifest_path=args.manifest, generated=args.generated,
            legacy_limit=args.legacy_limit,
            harvest_manifest_path=args.harvest_manifest,
            asset_catalog_path=args.asset_catalog)
    except (OSError, DocumentBuildError, DocStoreError) as exc:
        parser.exit(1, f'document store build failed: {exc}\n')
    print(json.dumps(result, sort_keys=True, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
