#!/usr/bin/env python3
"""Build checksum-bound Nevada WS9 grade and PP 610 evidence inputs.

This is a private evidence producer.  It verifies official NBMG/USGS PDFs,
checks reviewed quotations against the cited source pages, and writes inputs
accepted by ``build_national_grade_evidence.py``.  It never writes below
``site/`` and never changes a state registry, DONE flag, manifest, or release.

NBMG Mining District Files are image-only.  Their page-image SHA-256 is the
review boundary; deterministic OCR is only a search/check aid.  USGS text-layer
pages are checked directly.  Every derived page index is itself hashed and the
hash is carried by the national evidence source identity.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unicodedata
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

import build_national_grade_evidence as national


HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SITE = ROOT / 'site'
DEFAULT_SOURCES = ROOT / 'pipelines/config/nv_grade_sources.json'
DEFAULT_REVIEWED = ROOT / 'grades-research/nv/reviewed_grade_evidence.json'
DEFAULT_DISTRICTS = ROOT / 'grades-research/nv/pp610_district_inventory.json'
DEFAULT_OUTPUT = ROOT / 'build-inputs/ws9/nv-grade-evidence'
OFFICIAL_HOSTS = frozenset(('data.nbmg.unr.edu', 'collections.nbmg.unr.edu',
                            'pubs.usgs.gov'))
SOURCE_KEYS = frozenset((
    'source_id', 'title', 'authority', 'citation', 'publication_year',
    'catalog_url', 'document_url', 'local_path', 'bytes', 'sha256', 'pages',
    'text_mode'))
REVIEW_KEYS = frozenset((
    'schema_version', 'dataset', 'state', 'status', 'reviewed_on',
    'reviewed_by', 'review_method', 'mines'))
MINE_KEYS = frozenset(('mine_id', 'name', 'district', 'county', 'evidence'))
EVIDENCE_REQUIRED = frozenset((
    'evidence_id', 'source_id', 'pdf_page', 'page_cite', 'verbatim_quote',
    'quote_verbatim', 'measurements', 'basis', 'years'))
EVIDENCE_OPTIONAL = frozenset(('page_image_sha256',))
SHA_RE = re.compile(r'[0-9a-f]{64}')
ID_RE = re.compile(r'[a-z0-9][a-z0-9_.:-]{0,127}')
XHTML_NS = {'x': 'http://www.w3.org/1999/xhtml'}
MIN_QUOTE_MATCH = 0.65


class NevadaEvidenceError(ValueError):
    """A Nevada source or reviewed row violates the extraction contract."""


def canonical_bytes(value):
    try:
        return json.dumps(value, sort_keys=True, separators=(',', ':'),
                          ensure_ascii=False, allow_nan=False).encode('utf-8')
    except (TypeError, ValueError) as exc:
        raise NevadaEvidenceError(f'value is not canonical JSON: {exc}') from exc


def sha256_bytes(raw):
    return hashlib.sha256(raw).hexdigest()


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as source:
        for chunk in iter(lambda: source.read(1 << 20), b''):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path, label):
    try:
        document, raw = national.load_strict_json(str(path), label)
    except national.PublicationError as exc:
        raise NevadaEvidenceError(str(exc)) from exc
    return document, raw


def expect_keys(value, required, optional, label):
    if not isinstance(value, dict):
        raise NevadaEvidenceError(f'{label} must be an object')
    missing = sorted(set(required) - set(value))
    extra = sorted(set(value) - set(required) - set(optional))
    if missing or extra:
        raise NevadaEvidenceError(
            f'{label} keys mismatch: missing={missing}, extra={extra}')


def text(value, label, minimum=1, maximum=5000):
    if (not isinstance(value, str) or value != value.strip() or
            not minimum <= len(value) <= maximum or '\x00' in value):
        raise NevadaEvidenceError(
            f'{label} must be trimmed text of length {minimum}..{maximum}')
    return value


def identifier(value, label):
    text(value, label, maximum=128)
    if ID_RE.fullmatch(value) is None:
        raise NevadaEvidenceError(f'{label} must be a lowercase stable identifier')
    return value


def sha(value, label):
    if not isinstance(value, str) or SHA_RE.fullmatch(value) is None:
        raise NevadaEvidenceError(f'{label} must be a lowercase SHA-256')
    return value


def positive_number(value, label):
    if (not isinstance(value, (int, float)) or isinstance(value, bool) or
            not math.isfinite(value) or value <= 0):
        raise NevadaEvidenceError(f'{label} must be a positive finite number')


def is_inside(path, parent):
    try:
        return os.path.commonpath((str(path.resolve()), str(parent.resolve()))) == str(parent.resolve())
    except (OSError, ValueError):
        return False


def official_url(value, label):
    text(value, label, maximum=2048)
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme != 'https' or parsed.hostname not in OFFICIAL_HOSTS:
        raise NevadaEvidenceError(
            f'{label} must use HTTPS on an approved official NBMG/USGS host')
    if parsed.username or parsed.password or parsed.fragment:
        raise NevadaEvidenceError(f'{label} contains forbidden URL components')
    return value


def validate_source_inventory(document):
    expect_keys(document, ('schema_version', 'dataset', 'sources'), (),
                'Nevada source inventory')
    if document['schema_version'] != 1:
        raise NevadaEvidenceError('Nevada source inventory schema_version must be 1')
    if document['dataset'] != 'ws11-nevada-grade-source-inventory':
        raise NevadaEvidenceError('Nevada source inventory dataset identity is invalid')
    if not isinstance(document['sources'], list) or not document['sources']:
        raise NevadaEvidenceError('Nevada source inventory sources must be nonempty')
    sources = {}
    for index, row in enumerate(document['sources']):
        label = f'Nevada source inventory.sources[{index}]'
        expect_keys(row, SOURCE_KEYS, (), label)
        source_id = identifier(row['source_id'], f'{label}.source_id')
        if source_id in sources:
            raise NevadaEvidenceError(f'duplicate source_id {source_id}')
        for field in ('title', 'authority', 'citation'):
            text(row[field], f'{label}.{field}', minimum=3, maximum=1000)
        year = row['publication_year']
        if (not isinstance(year, int) or isinstance(year, bool) or
                not 1800 <= year <= 2100):
            raise NevadaEvidenceError(f'{label}.publication_year is invalid')
        official_url(row['catalog_url'], f'{label}.catalog_url')
        official_url(row['document_url'], f'{label}.document_url')
        local = Path(text(row['local_path'], f'{label}.local_path', maximum=500))
        if local.is_absolute() or '..' in local.parts or '.' in local.parts:
            raise NevadaEvidenceError(f'{label}.local_path must be a normalized relative path')
        resolved = (ROOT / local).resolve()
        if not is_inside(resolved, ROOT / 'pipelines/cache/nv-grade-sources'):
            raise NevadaEvidenceError(
                f'{label}.local_path must stay in pipelines/cache/nv-grade-sources')
        if not isinstance(row['bytes'], int) or isinstance(row['bytes'], bool) or row['bytes'] <= 0:
            raise NevadaEvidenceError(f'{label}.bytes must be a positive integer')
        sha(row['sha256'], f'{label}.sha256')
        if not isinstance(row['pages'], int) or isinstance(row['pages'], bool) or row['pages'] <= 0:
            raise NevadaEvidenceError(f'{label}.pages must be a positive integer')
        if row['text_mode'] not in ('embedded', 'ocr'):
            raise NevadaEvidenceError(f'{label}.text_mode must be embedded or ocr')
        sources[source_id] = dict(row, resolved_path=resolved)
    if 'pp610' not in sources:
        raise NevadaEvidenceError('Nevada source inventory must include source_id pp610')
    if sources['pp610']['text_mode'] != 'embedded':
        raise NevadaEvidenceError('PP 610 must use its official PDF text layer')
    return sources


def validate_reviewed(document, sources):
    expect_keys(document, REVIEW_KEYS, (), 'reviewed Nevada grade evidence')
    if (document['schema_version'] != 1 or document['state'] != 'NV' or
            document['status'] != 'reviewed'):
        raise NevadaEvidenceError(
            'reviewed Nevada evidence must be schema 1, state NV, status reviewed')
    for field in ('dataset', 'reviewed_on', 'reviewed_by', 'review_method'):
        text(document[field], f'reviewed Nevada evidence.{field}', minimum=3)
    mines = document['mines']
    if not isinstance(mines, list) or len(mines) < 25:
        raise NevadaEvidenceError(
            f'reviewed Nevada evidence has {len(mines) if isinstance(mines, list) else 0} mines; at least 25 are required')
    mine_ids = set()
    evidence_ids = set()
    mine_names = set()
    used_sources = set()
    out = []
    for mine_index, mine in enumerate(mines):
        label = f'reviewed Nevada evidence.mines[{mine_index}]'
        expect_keys(mine, MINE_KEYS, (), label)
        mine_id = identifier(mine['mine_id'], f'{label}.mine_id')
        if mine_id in mine_ids:
            raise NevadaEvidenceError(f'duplicate mine_id {mine_id}')
        mine_ids.add(mine_id)
        for field in ('name', 'district', 'county'):
            text(mine[field], f'{label}.{field}', minimum=2, maximum=300)
        name_key = (re.sub(r'[^a-z0-9]+', ' ', mine['name'].lower()).strip(),
                    re.sub(r'[^a-z0-9]+', ' ', mine['district'].lower()).strip())
        if name_key in mine_names:
            raise NevadaEvidenceError(f'duplicate normalized mine/district {mine["name"]!r}')
        mine_names.add(name_key)
        if not isinstance(mine['evidence'], list) or not mine['evidence']:
            raise NevadaEvidenceError(f'{label}.evidence must be nonempty')
        evidence_out = []
        for ev_index, evidence in enumerate(mine['evidence']):
            ev_label = f'{label}.evidence[{ev_index}]'
            expect_keys(evidence, EVIDENCE_REQUIRED, EVIDENCE_OPTIONAL, ev_label)
            evidence_id = identifier(evidence['evidence_id'], f'{ev_label}.evidence_id')
            if evidence_id in evidence_ids:
                raise NevadaEvidenceError(f'duplicate evidence_id {evidence_id}')
            evidence_ids.add(evidence_id)
            source_id = identifier(evidence['source_id'], f'{ev_label}.source_id')
            if source_id not in sources or source_id == 'pp610':
                raise NevadaEvidenceError(f'{ev_label} references unknown/non-grade source {source_id}')
            used_sources.add(source_id)
            source = sources[source_id]
            page = evidence['pdf_page']
            if (not isinstance(page, int) or isinstance(page, bool) or
                    not 1 <= page <= source['pages']):
                raise NevadaEvidenceError(f'{ev_label}.pdf_page is outside the source')
            page_cite = text(evidence['page_cite'], f'{ev_label}.page_cite', minimum=2,
                             maximum=200)
            if not any(character.isdigit() for character in page_cite):
                raise NevadaEvidenceError(f'{ev_label}.page_cite must contain a page number')
            text(evidence['verbatim_quote'], f'{ev_label}.verbatim_quote', minimum=8)
            if evidence['quote_verbatim'] is not True:
                raise NevadaEvidenceError(f'{ev_label}.quote_verbatim must be true')
            measurements = evidence['measurements']
            if not isinstance(measurements, list) or not measurements:
                raise NevadaEvidenceError(
                    f'{ev_label} must contain at least one native measurement')
            seen_commodities = set()
            for measurement_index, measurement in enumerate(measurements):
                measurement_label = f'{ev_label}.measurements[{measurement_index}]'
                expect_keys(measurement, ('commodity', 'value', 'unit'), (),
                            measurement_label)
                commodity = measurement['commodity']
                if commodity not in national.COMMODITIES or commodity in seen_commodities:
                    raise NevadaEvidenceError(
                        f'{measurement_label}.commodity is unsupported or duplicated')
                seen_commodities.add(commodity)
                positive_number(measurement['value'], f'{measurement_label}.value')
                if measurement['unit'] not in national.NATIVE_UNITS[commodity]:
                    raise NevadaEvidenceError(f'{measurement_label}.unit is invalid')
            for field in ('basis', 'years'):
                text(evidence[field], f'{ev_label}.{field}', maximum=500)
            if source['text_mode'] == 'ocr':
                if 'page_image_sha256' not in evidence:
                    raise NevadaEvidenceError(
                        f'{ev_label} needs the reviewed image-only page SHA-256')
                sha(evidence['page_image_sha256'], f'{ev_label}.page_image_sha256')
            elif 'page_image_sha256' in evidence:
                raise NevadaEvidenceError(
                    f'{ev_label} must not add an image hash to a text-layer source')
            evidence_out.append(dict(evidence))
        mine_out = {key: mine[key] for key in ('mine_id', 'name', 'district', 'county')}
        mine_out['evidence'] = evidence_out
        out.append(mine_out)
    if len(used_sources) < 2:
        raise NevadaEvidenceError('Nevada reviewed grades must use at least two primary sources')
    return out, used_sources


def validate_district_inventory(document, sources):
    expect_keys(document,
                ('schema_version', 'dataset', 'state', 'source_id',
                 'review_scope', 'districts'), (), 'Nevada PP 610 inventory')
    if (document['schema_version'] != 1 or document['state'] != 'NV' or
            document['source_id'] != 'pp610'):
        raise NevadaEvidenceError('Nevada PP 610 inventory identity is invalid')
    for field in ('dataset', 'review_scope'):
        text(document[field], f'Nevada PP 610 inventory.{field}', minimum=8)
    rows = document['districts']
    if not isinstance(rows, list) or len(rows) != 71:
        raise NevadaEvidenceError(
            'Nevada PP 610 inventory must contain all 71 Figure 16 districts')
    ids = set()
    names = set()
    expected_counties = {
        'Churchill', 'Clark', 'Elko', 'Esmeralda', 'Eureka', 'Humboldt',
        'Lander', 'Lincoln', 'Lyon', 'Mineral', 'Nye', 'Pershing', 'Storey',
        'Washoe', 'White Pine'}
    out = []
    source = sources['pp610']
    for index, row in enumerate(rows):
        label = f'Nevada PP 610 inventory.districts[{index}]'
        expect_keys(row, ('district_id', 'name', 'county', 'pdf_page',
                          'source_heading'), (), label)
        district_id = identifier(row['district_id'], f'{label}.district_id')
        if district_id in ids:
            raise NevadaEvidenceError(f'duplicate PP 610 district_id {district_id}')
        ids.add(district_id)
        name = text(row['name'], f'{label}.name', minimum=2, maximum=300)
        name_key = re.sub(r'[^a-z0-9]+', ' ', name.lower()).strip()
        if name_key in names:
            raise NevadaEvidenceError(f'duplicate PP 610 district name {name!r}')
        names.add(name_key)
        if row['county'] not in expected_counties:
            raise NevadaEvidenceError(f'{label}.county is outside reviewed Nevada scope')
        page = row['pdf_page']
        if (not isinstance(page, int) or isinstance(page, bool) or
                not 177 <= page <= 206 or page > source['pages']):
            raise NevadaEvidenceError(f'{label}.pdf_page is outside Nevada chapter')
        text(row['source_heading'], f'{label}.source_heading', minimum=8,
             maximum=200)
        out.append(dict(row))
    return out


def verify_pdf(source):
    path = source['resolved_path']
    if path.is_symlink():
        raise NevadaEvidenceError(f'{source["source_id"]} PDF must not be a symlink')
    try:
        actual_bytes = path.stat().st_size
        actual_sha = sha256_file(path)
    except OSError as exc:
        raise NevadaEvidenceError(
            f'{source["source_id"]} PDF unavailable; run fetch: {exc}') from exc
    if actual_bytes != source['bytes'] or actual_sha != source['sha256']:
        raise NevadaEvidenceError(
            f'{source["source_id"]} source drift: expected '
            f'{source["bytes"]}/{source["sha256"]}, got {actual_bytes}/{actual_sha}')
    if not shutil.which('pdfinfo'):
        raise NevadaEvidenceError('pdfinfo is required to verify source page counts')
    process = subprocess.run(['pdfinfo', str(path)], capture_output=True,
                             text=True, check=False)
    if process.returncode:
        raise NevadaEvidenceError(
            f'pdfinfo rejected {source["source_id"]}: {process.stderr.strip()}')
    match = re.search(r'^Pages:\s+(\d+)\s*$', process.stdout, re.MULTILINE)
    if match is None or int(match.group(1)) != source['pages']:
        raise NevadaEvidenceError(
            f'{source["source_id"]} page count differs from the reviewed inventory')


def fetch_source(source):
    path = source['resolved_path']
    if path.exists():
        verify_pdf(source)
        return 'verified'
    path.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(
        source['document_url'], headers={'User-Agent': 'nw-mineral-monitor-ws11/1'})
    temp_path = path.with_name(path.name + '.part')
    try:
        with urllib.request.urlopen(request, timeout=90) as response, open(temp_path, 'wb') as output:
            final = urllib.parse.urlparse(response.geturl())
            if final.scheme != 'https' or final.hostname not in OFFICIAL_HOSTS:
                raise NevadaEvidenceError(
                    f'{source["source_id"]} redirected outside approved official hosts')
            remaining = source['bytes'] + 1
            while remaining:
                chunk = response.read(min(1 << 20, remaining))
                if not chunk:
                    break
                output.write(chunk)
                remaining -= len(chunk)
        if temp_path.stat().st_size != source['bytes'] or sha256_file(temp_path) != source['sha256']:
            raise NevadaEvidenceError(
                f'{source["source_id"]} download does not match reviewed bytes/SHA-256')
        with open(temp_path, 'rb') as downloaded:
            magic = downloaded.read(5)
        if magic != b'%PDF-':
            raise NevadaEvidenceError(f'{source["source_id"]} response is not a PDF')
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink()
    verify_pdf(source)
    return 'downloaded'


def run_tool(arguments, label):
    process = subprocess.run(arguments, capture_output=True, check=False)
    if process.returncode:
        error = process.stderr.decode('utf-8', 'replace').strip()
        raise NevadaEvidenceError(f'{label} failed: {error}')
    return process.stdout


def extract_text_page(source, pdf_page):
    if not shutil.which('pdftotext'):
        raise NevadaEvidenceError('pdftotext is required for cited-page extraction')
    return run_tool([
        'pdftotext', '-f', str(pdf_page), '-l', str(pdf_page), '-enc', 'UTF-8',
        '-layout', str(source['resolved_path']), '-'],
        f'pdftotext {source["source_id"]} page {pdf_page}')


def extract_ocr_page(source, pdf_page):
    for tool in ('pdftoppm', 'tesseract'):
        if not shutil.which(tool):
            raise NevadaEvidenceError(f'{tool} is required for image-only NBMG pages')
    with tempfile.TemporaryDirectory(prefix='nv-grade-page-') as directory:
        base = Path(directory) / 'page'
        run_tool([
            'pdftoppm', '-f', str(pdf_page), '-l', str(pdf_page), '-r', '300',
            '-png', '-singlefile', str(source['resolved_path']), str(base)],
            f'pdftoppm {source["source_id"]} page {pdf_page}')
        image = base.with_suffix('.png')
        if not image.is_file():
            raise NevadaEvidenceError('pdftoppm did not create the reviewed page image')
        image_sha = sha256_file(image)
        # Tesseract on macOS is sensitive to /var -> /private/var path aliases;
        # execute from the render directory with a basename.
        process = subprocess.run(
            ['tesseract', image.name, 'stdout', '--psm', '6'], cwd=directory,
            capture_output=True, check=False)
        if process.returncode:
            raise NevadaEvidenceError(
                f'tesseract {source["source_id"]} page {pdf_page} failed: '
                f'{process.stderr.decode("utf-8", "replace").strip()}')
        return process.stdout, image_sha


def normalized_words(value):
    value = unicodedata.normalize('NFKD', value).lower()
    value = re.sub(r'-\s*\n\s*', '', value)
    value = re.sub(r'(?<=\d)\s*\.\s*(?=\d)', '.', value)
    return re.findall(r'[a-z0-9$%.]+', value)


def quote_match_score(quote, page_text):
    quote_words = normalized_words(quote)
    page_words = normalized_words(page_text)
    if not quote_words or not page_words:
        return 0.0
    quote_text = ' '.join(quote_words)
    if quote_text in ' '.join(page_words):
        return 1.0
    width = len(quote_words)
    best = 0.0
    for index in range(max(1, len(page_words) - width + 1)):
        window = set(page_words[index:index + max(width + 1, int(width * 1.3))])
        match = sum(1 for word in quote_words if word in window) / width
        best = max(best, match)
    return round(best, 6)


def quote_word_coverage(quote, page_text):
    """Order-independent cross-check for PP 610's interleaved two columns."""
    quote_counts = Counter(normalized_words(quote))
    page_counts = Counter(normalized_words(page_text))
    total = sum(quote_counts.values())
    if not total:
        return 0.0
    covered = sum(min(count, page_counts[word])
                  for word, count in quote_counts.items())
    return round(covered / total, 6)


def bbox_blocks(source, pdf_page):
    raw = run_tool([
        'pdftotext', '-f', str(pdf_page), '-l', str(pdf_page), '-bbox-layout',
        str(source['resolved_path']), '-'],
        f'pdftotext bbox {source["source_id"]} page {pdf_page}')
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise NevadaEvidenceError(
            f'cannot parse PP 610 page {pdf_page} bounding boxes: {exc}') from exc
    blocks = []
    for block in root.findall('.//x:block', XHTML_NS):
        words = [word.text or '' for word in block.findall('.//x:word', XHTML_NS)]
        value = ' '.join(word for word in words if word).strip()
        if value:
            blocks.append({
                'x_min': float(block.attrib['xMin']),
                'x_max': float(block.attrib['xMax']),
                'x_center': (float(block.attrib['xMin']) +
                             float(block.attrib['xMax'])) / 2,
                'y_min': float(block.attrib['yMin']),
                'text': value,
            })
    return blocks


def first_district_sentence(blocks, heading, district_id):
    matches = [block for block in blocks if block['text'] == heading]
    if len(matches) != 1:
        raise NevadaEvidenceError(
            f'{district_id}: expected exactly one PP 610 heading {heading!r}, found {len(matches)}')
    heading_block = matches[0]
    left_column = heading_block['x_center'] < 330
    candidates = [block for block in blocks
                  if (block['x_center'] < 330) == left_column and
                  block['y_min'] > heading_block['y_min'] + 1 and
                  block['y_min'] < 760 and
                  'PRINCIPAL GOLD-PRODUCING' not in block['text']]
    candidates.sort(key=lambda block: block['y_min'])
    # A district can start at the foot of a page (Jackson).  Continue at the
    # top of the same column on the next PDF page before giving up.
    def finish(rows):
        words = []
        prior_y = None
        for block in rows:
            if prior_y is not None and block['y_min'] - prior_y > 50:
                break
            words.extend(block['text'].split())
            prior_y = block['y_min']
            joined = ' '.join(words)
            # Prefer punctuation followed by a new capitalized prose sentence;
            # this avoids treating abbreviations such as ``T. 38 N.`` as stops.
            matches = list(re.finditer(r'[.!?][\"\']?(?=\s+[A-Z][a-z]|$)', joined))
            if matches:
                match = next((candidate for candidate in matches
                              if candidate.end() >= 30), None)
                if match is not None:
                    quote = joined[:match.end()].strip()
                    if len(quote) >= 30:
                        return quote
            if len(joined) > 1500:
                break
        return None

    quote = finish(candidates)
    if quote is not None:
        return quote
    fragment = candidates[0]['text'].strip() if candidates else ''
    if len(fragment) >= 30:
        # A heading at the foot of a page can have no complete sentence on the
        # cited page.  Preserve the complete source-page fragment instead of
        # silently binding next-page text to the wrong page hash.
        return fragment
    raise NevadaEvidenceError(
        f'{district_id}: could not derive a first sentence after {heading!r}')


def page_record(source, page, page_cache):
    cache_key = (source['source_id'], page)
    if cache_key not in page_cache:
        if source['text_mode'] == 'ocr':
            raw, image_sha = extract_ocr_page(source, page)
        else:
            raw = extract_text_page(source, page)
            image_sha = None
        decoded = raw.decode('utf-8', 'replace')
        page_cache[cache_key] = {
            'raw': raw,
            'text': decoded,
            'page_text_sha256': sha256_bytes(raw),
            'page_image_sha256': image_sha,
        }
    return page_cache[cache_key]


def source_identity(source, page_index_sha):
    return {
        'source_id': source['source_id'],
        'title': source['title'],
        'authority': source['authority'],
        'url': source['document_url'],
        'primary': True,
        'document_sha256': source['sha256'],
        'page_index_sha256': page_index_sha,
        'citation': source['citation'],
        'publication_year': source['publication_year'],
    }


def atomic_json(path, document):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or path.parent.is_symlink():
        raise NevadaEvidenceError(f'output path must not be a symlink: {path}')
    raw = canonical_bytes(document)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=path.name + '.',
                                     suffix='.part', delete=False) as output:
        output.write(raw)
        temp_name = output.name
    os.replace(temp_name, path)
    return {'path': str(path), 'bytes': len(raw), 'sha256': sha256_bytes(raw)}


def tool_version(command):
    if not shutil.which(command):
        return None
    process = subprocess.run([command, '-v'] if command != 'tesseract' else
                             [command, '--version'], capture_output=True,
                             text=True, check=False)
    value = (process.stdout or process.stderr).splitlines()
    return value[0].strip() if value else 'unknown'


def build(sources_path=DEFAULT_SOURCES, reviewed_path=DEFAULT_REVIEWED,
          districts_path=DEFAULT_DISTRICTS, output=DEFAULT_OUTPUT):
    sources_document, sources_raw = load_json(Path(sources_path), 'Nevada source inventory')
    reviewed_document, reviewed_raw = load_json(Path(reviewed_path), 'reviewed Nevada grade evidence')
    districts_document, districts_raw = load_json(Path(districts_path), 'Nevada PP 610 inventory')
    sources = validate_source_inventory(sources_document)
    mines, grade_source_ids = validate_reviewed(reviewed_document, sources)
    districts = validate_district_inventory(districts_document, sources)
    required_source_ids = set(grade_source_ids) | {'pp610'}
    for source_id in sorted(required_source_ids):
        verify_pdf(sources[source_id])

    output = Path(output).resolve()
    if is_inside(output, SITE):
        raise NevadaEvidenceError('Nevada raw evidence output must stay outside site/')
    if output == ROOT or not is_inside(output, ROOT):
        raise NevadaEvidenceError('Nevada raw evidence output must stay inside the workspace')
    page_cache = {}
    source_page_rows = {source_id: {} for source_id in required_source_ids}

    grade_mines = []
    for mine in mines:
        out_mine = {key: mine[key] for key in ('mine_id', 'name', 'district', 'county')}
        out_mine['evidence'] = []
        for evidence in mine['evidence']:
            source = sources[evidence['source_id']]
            page = page_record(source, evidence['pdf_page'], page_cache)
            if source['text_mode'] == 'ocr':
                if page['page_image_sha256'] != evidence['page_image_sha256']:
                    raise NevadaEvidenceError(
                        f'{evidence["evidence_id"]}: reviewed NBMG page-image SHA-256 changed')
            score = quote_match_score(evidence['verbatim_quote'], page['text'])
            if source['text_mode'] == 'embedded' and score < MIN_QUOTE_MATCH:
                raise NevadaEvidenceError(
                    f'{evidence["evidence_id"]}: quote/page match {score:.3f} is below '
                    f'{MIN_QUOTE_MATCH:.2f}')
            row = {
                'evidence_id': evidence['evidence_id'],
                'source_id': evidence['source_id'],
                'page_cite': evidence['page_cite'],
                'verbatim_quote': evidence['verbatim_quote'],
                'quote_verbatim': True,
                'page_text_sha256': page['page_text_sha256'],
                'measurements': evidence['measurements'],
                'basis': evidence['basis'],
                'years': evidence['years'],
            }
            out_mine['evidence'].append(row)
            page_row = source_page_rows[evidence['source_id']].setdefault(
                evidence['pdf_page'], {
                    'pdf_page': evidence['pdf_page'],
                    'page_text_sha256': page['page_text_sha256'],
                    'text_mode': source['text_mode'],
                    'page_image_sha256': page['page_image_sha256'],
                    'checks': [],
                })
            page_row['checks'].append({
                'evidence_id': evidence['evidence_id'],
                'page_cite': evidence['page_cite'],
                'quote_match_score': score,
                'review_boundary': ('page_image_sha256'
                                    if source['text_mode'] == 'ocr'
                                    else 'page_text_sha256_and_quote_match'),
            })
        grade_mines.append(out_mine)

    page_index_artifacts = {}
    source_identities = {}
    for source_id in sorted(grade_source_ids):
        source = sources[source_id]
        page_index = {
            'schema_version': 1,
            'source_id': source_id,
            'document_sha256': source['sha256'],
            'extraction': {
                'text_mode': source['text_mode'],
                'pdftotext_arguments': '-enc UTF-8 -layout',
                'ocr_arguments': ('pdftoppm -r 300 -png; tesseract --psm 6'
                                  if source['text_mode'] == 'ocr' else None),
                'ocr_role': ('search_cross_check_only_page_image_sha_is_review_boundary'
                             if source['text_mode'] == 'ocr' else None),
            },
            'pages': [source_page_rows[source_id][page]
                      for page in sorted(source_page_rows[source_id])],
        }
        index_sha = sha256_bytes(canonical_bytes(page_index))
        index_path = output / 'page-indexes' / f'{source_id}.{index_sha}.json'
        page_index_artifacts[source_id] = atomic_json(index_path, page_index)
        source_identities[source_id] = source_identity(source, index_sha)

    grades_document = {
        'schema_version': 1,
        'state': 'NV',
        'sources': [source_identities[source_id] for source_id in sorted(source_identities)],
        'mines': grade_mines,
    }
    try:
        grade_validation = national.validate_grade_document(
            grades_document, 'NV', {}, '0' * 64)
    except national.PublicationError as exc:
        raise NevadaEvidenceError(f'national grade contract rejected Nevada: {exc}') from exc
    grades_path = output / 'grades/nv.json'
    grades_artifact = atomic_json(grades_path, grades_document)

    pp_source = sources['pp610']
    bbox_cache = {}
    pp_districts = []
    pp_page_rows = {}
    for district in districts:
        pdf_page = district['pdf_page']
        page = page_record(pp_source, pdf_page, page_cache)
        if pdf_page not in bbox_cache:
            bbox_cache[pdf_page] = bbox_blocks(pp_source, pdf_page)
        quote = first_district_sentence(
            bbox_cache[pdf_page], district['source_heading'], district['district_id'])
        score = quote_word_coverage(quote, page['text'])
        # The layout text layer interleaves two columns on some PP 610 pages.
        # The quote itself comes from ordered bbox words; whole-page word
        # coverage is therefore the independent cross-check of that extraction.
        if score < 0.85:
            raise NevadaEvidenceError(
                f'{district["district_id"]}: derived PP 610 quote/page match {score:.3f}')
        page_cite = f'p. {pdf_page - 6}'
        pp_districts.append({
            'district_id': district['district_id'],
            'name': district['name'],
            'page_cite': page_cite,
            'verbatim_quote': quote,
            'quote_verbatim': True,
            'page_text_sha256': page['page_text_sha256'],
        })
        page_row = pp_page_rows.setdefault(pdf_page, {
            'pdf_page': pdf_page,
            'printed_page': pdf_page - 6,
            'page_text_sha256': page['page_text_sha256'],
            'text_mode': 'embedded',
            'checks': [],
        })
        page_row['checks'].append({
            'district_id': district['district_id'],
            'source_heading': district['source_heading'],
            'quote_match_score': score,
        })
    pp_index = {
        'schema_version': 1,
        'source_id': 'pp610',
        'document_sha256': pp_source['sha256'],
        'reviewed_scope': {'pdf_pages': '177-206', 'printed_pages': '171-200'},
        'extraction': {
            'page_text': 'pdftotext -enc UTF-8 -layout',
            'quote': 'first complete sentence after exact bbox district heading',
        },
        'pages': [pp_page_rows[page] for page in sorted(pp_page_rows)],
    }
    pp_index_sha = sha256_bytes(canonical_bytes(pp_index))
    pp_index_path = output / 'page-indexes' / f'pp610.{pp_index_sha}.json'
    page_index_artifacts['pp610'] = atomic_json(pp_index_path, pp_index)
    pp_document = {
        'schema_version': 1,
        'state': 'NV',
        'complete': True,
        'source': source_identity(pp_source, pp_index_sha),
        'districts': pp_districts,
    }
    try:
        pp_validation = national.validate_pp610_document(pp_document, 'NV')
    except national.PublicationError as exc:
        raise NevadaEvidenceError(f'national PP 610 contract rejected Nevada: {exc}') from exc
    pp_path = output / 'pp610/nv.json'
    pp_artifact = atomic_json(pp_path, pp_document)

    relative = lambda path: str(Path(path).resolve().relative_to(output))
    for artifact in [grades_artifact, pp_artifact, *page_index_artifacts.values()]:
        artifact['path'] = relative(artifact['path'])
    report = {
        'schema_version': 1,
        'dataset': 'ws11-nevada-grade-evidence-build',
        'state': 'NV',
        'effect': 'evidence_only_no_release_or_done_mutation',
        'inputs': {
            'producer_sha256': sha256_file(Path(__file__).resolve()),
            'national_contract_sha256': sha256_file(
                ROOT / 'pipelines/build_national_grade_evidence.py'),
            'source_inventory_sha256': sha256_bytes(sources_raw),
            'reviewed_grade_evidence_sha256': sha256_bytes(reviewed_raw),
            'pp610_district_inventory_sha256': sha256_bytes(districts_raw),
        },
        'source_documents': [{
            'source_id': source_id,
            'bytes': sources[source_id]['bytes'],
            'sha256': sources[source_id]['sha256'],
            'url': sources[source_id]['document_url'],
        } for source_id in sorted(required_source_ids)],
        'toolchain': {
            'python': sys.version.split()[0],
            'pdftotext': tool_version('pdftotext'),
            'pdfinfo': tool_version('pdfinfo'),
            'pdftoppm': tool_version('pdftoppm'),
            'tesseract': tool_version('tesseract'),
        },
        'metrics': {
            **grade_validation['metrics'],
            'pp610_districts': pp_validation['district_count'],
            'nbmg_image_pages_review_bound': sum(
                1 for page in page_cache.values() if page['page_image_sha256']),
        },
        'threshold_observation': {
            'at_least_25_graded_mines': grade_validation['metrics']['graded_mines'] >= 25,
            'at_least_2_primary_sources': grade_validation['metrics']['primary_sources'] >= 2,
            'complete_pp610_anchor': pp_validation['district_count'] == 71,
            'is_release_decision': False,
        },
        'artifacts': {
            'grade_input': grades_artifact,
            'pp610_input': pp_artifact,
            'page_indexes': page_index_artifacts,
        },
        'unconsumed_inventory_sources': sorted(set(sources) - required_source_ids),
    }
    report_artifact = atomic_json(output / 'build.json', report)
    return report, report_artifact


def check_inputs(sources_path=DEFAULT_SOURCES, reviewed_path=DEFAULT_REVIEWED,
                 districts_path=DEFAULT_DISTRICTS):
    sources_document, _ = load_json(Path(sources_path), 'Nevada source inventory')
    reviewed_document, _ = load_json(Path(reviewed_path), 'reviewed Nevada grade evidence')
    districts_document, _ = load_json(Path(districts_path), 'Nevada PP 610 inventory')
    sources = validate_source_inventory(sources_document)
    mines, used = validate_reviewed(reviewed_document, sources)
    districts = validate_district_inventory(districts_document, sources)
    return {'mines': len(mines), 'grade_sources': len(used),
            'pp610_districts': len(districts)}


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument('--sources', default=str(DEFAULT_SOURCES))
    result.add_argument('--reviewed', default=str(DEFAULT_REVIEWED))
    result.add_argument('--districts', default=str(DEFAULT_DISTRICTS))
    subparsers = result.add_subparsers(dest='command', required=True)
    subparsers.add_parser('check', help='validate review manifests without source PDFs')
    fetch = subparsers.add_parser('fetch', help='fetch/verify official pinned PDFs')
    fetch.add_argument('--all', action='store_true',
                       help='also fetch inventory candidates unused by this build')
    build_parser = subparsers.add_parser('build', help='verify pages and produce private inputs')
    build_parser.add_argument('--output', default=str(DEFAULT_OUTPUT))
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command == 'check':
            result = check_inputs(args.sources, args.reviewed, args.districts)
        elif args.command == 'fetch':
            sources_document, _ = load_json(Path(args.sources), 'Nevada source inventory')
            reviewed_document, _ = load_json(Path(args.reviewed), 'reviewed Nevada grade evidence')
            districts_document, _ = load_json(Path(args.districts), 'Nevada PP 610 inventory')
            sources = validate_source_inventory(sources_document)
            _, used = validate_reviewed(reviewed_document, sources)
            validate_district_inventory(districts_document, sources)
            source_ids = set(sources) if args.all else set(used) | {'pp610'}
            result = {source_id: fetch_source(sources[source_id])
                      for source_id in sorted(source_ids)}
        else:
            report, artifact = build(args.sources, args.reviewed, args.districts,
                                     args.output)
            result = {'metrics': report['metrics'], 'build': artifact}
    except (NevadaEvidenceError, OSError) as exc:
        print(f'Nevada grade evidence ERROR: {exc}', file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
