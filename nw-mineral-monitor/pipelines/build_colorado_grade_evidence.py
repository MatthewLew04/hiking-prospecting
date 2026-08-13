#!/usr/bin/env python3
"""Build checksum-bound Colorado WS11 grade and PP 610 evidence inputs.

This producer is evidence-only.  It verifies official USGS PDFs and every
reviewed source-page quotation, emits inputs accepted by the national grade
compiler, and never writes below ``site/`` or changes registry/release state.

Colorado's grade rows use native measurements from USGS Professional Paper
359 and Bulletin 478.  The PP 610 inventory is the complete 44-district
Figure 10 legend, not a sample of better-known districts.
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
import urllib.parse
import urllib.request
from pathlib import Path

import build_national_grade_evidence as national
import build_nevada_grade_evidence as extraction


HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SITE = ROOT / 'site'
DEFAULT_SOURCES = ROOT / 'pipelines/config/co_grade_sources.json'
DEFAULT_REVIEWED = ROOT / 'grades-research/co/reviewed_grade_evidence.json'
DEFAULT_DISTRICTS = ROOT / 'grades-research/co/pp610_district_inventory.json'
DEFAULT_OUTPUT = ROOT / 'build-inputs/ws9/co-grade-evidence'
OFFICIAL_HOSTS = frozenset(('pubs.usgs.gov',))
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
MIN_QUOTE_MATCH = 0.65

# Figure 10 is the review boundary for completeness and spelling.  Requiring
# this exact ordered identity prevents a superficially plausible subset from
# passing the Colorado check.
PP610_FIGURE_10 = (
    ('Clear Creek placers (Adams County)', 'Adams'),
    ('Jamestown', 'Boulder'),
    ('Gold Hill-Sugarloaf', 'Boulder'),
    ('Ward', 'Boulder'),
    ('Magnolia', 'Boulder'),
    ('Grand Island-Caribou', 'Boulder'),
    ('Chalk Creek', 'Chaffee'),
    ('Monarch', 'Chaffee'),
    ('Alice', 'Clear Creek'),
    ('Empire', 'Clear Creek'),
    ('Idaho Springs', 'Clear Creek'),
    ('Freeland-Lamartine', 'Clear Creek'),
    ('Georgetown-Silver Plume', 'Clear Creek'),
    ('Argentine', 'Clear Creek'),
    ('Rosita Hills', 'Custer'),
    ('Rico', 'Dolores'),
    ('Gilman', 'Eagle'),
    ('Northern Gilpin', 'Gilpin'),
    ('Central City', 'Gilpin'),
    ('Gold Brick-Quartz Creek', 'Gunnison'),
    ('Tincup', 'Gunnison'),
    ('Lake City', 'Hinsdale'),
    ('Clear Creek placers (Jefferson County)', 'Jefferson'),
    ('Leadville', 'Lake'),
    ('Arkansas River valley placers', 'Lake'),
    ('La Plata', 'La Plata'),
    ('Creede', 'Mineral'),
    ('Sneffels-Red Mountain', 'Ouray'),
    ('Uncompahgre', 'Ouray'),
    ('Alma', 'Park'),
    ('Fairplay', 'Park'),
    ('Tarryall', 'Park'),
    ('Independence Pass', 'Pitkin'),
    ('Summitville', 'Rio Grande'),
    ('Hahns Peak', 'Routt'),
    ('Bonanza', 'Saguache'),
    ('Animas', 'San Juan'),
    ('Eureka', 'San Juan'),
    ('Ophir', 'San Miguel'),
    ('Telluride', 'San Miguel'),
    ('Mount Wilson', 'San Miguel'),
    ('Breckenridge', 'Summit'),
    ('Tenmile', 'Summit'),
    ('Cripple Creek', 'Teller'),
)
PP610_DESCRIPTION_LOCATORS = (
    (93, None),
    (95, 'JAMESTOWN DISTRICT'),
    (94, 'GOLD HILL-SUGARLOAF DISTRICT'),
    (96, 'WARD DISTRICT'),
    (96, 'MAGNOLIA DISTRICT'),
    (95, 'GRAND ISLAND-CARIBOU DISTRICT'),
    (98, 'CHALK CREim DISTRICT'),
    (98, 'MONARCH DISTRICT'),
    (99, None),
    (100, 'EMPIRE DISTRICT'),
    (102, 'IDAHO SPRINGS DISTRICT'),
    (101, 'FREEJ.AND-J,AMARTINJ<: DISTRICT'),
    (101, 'GEORGETOWN-SILVER PLUME DISTRICT'),
    (100, 'ARGENTINE DISTRICT'),
    (103, 'ROSITA HILLS DISTRICT'),
    (104, 'RICO DISTRICT'),
    (105, 'GILMAN DISTRICT'),
    (106, 'NORTHERN GILPIN DISTRICT'),
    (106, 'CENTRAL CITY DISTRICT'),
    (107, 'GOLD BRICK.QUARTZ CREEK DISTRICT'),
    (108, 'TINCUP DISTRICT'),
    (109, 'LAKE CITY DISTRICT'),
    (109, None),
    (110, 'LEADVILLE DISTRICT'),
    (110, 'ARKANSAS RIVER VALLEY PLACERS'),
    (111, 'LA PLATA DISTRICT'),
    (112, 'CREEDE DISTRICT'),
    (113, 'SNEFFELS-RED MOUNTAIN DISTRICT'),
    (114, 'UNCOMPAHGRE, DISTRICT'),
    (115, 'AL.M:A DISTRICT'),
    (116, 'FAIRPLAY DISTRICT'),
    (116, None),
    (116, 'INDEPENDENCE PASS DISTRICT'),
    (117, 'SUMMITVILLE DISTRICT'),
    (118, 'HAHNS PEAK DISTRICT'),
    (118, 'BONANZA DISTRICT'),
    (119, 'ANIMAS DISTRICT'),
    (120, 'EUREKA DISTRICT'),
    (121, 'OPHIR DISTRICT'),
    (121, 'TELLURIDE DISTRICT'),
    (120, 'MOUNT WILSON DISTRICT'),
    (122, 'BRECKENRIDGE DISTRICT'),
    (123, 'TENMILE DISTRICT'),
    (123, 'CRIPPLE GREEK DISTRICT'),
)


class ColoradoEvidenceError(ValueError):
    """A Colorado source or reviewed row violates the evidence contract."""


def canonical_bytes(value):
    try:
        return json.dumps(value, sort_keys=True, separators=(',', ':'),
                          ensure_ascii=False, allow_nan=False).encode('utf-8')
    except (TypeError, ValueError) as exc:
        raise ColoradoEvidenceError(f'value is not canonical JSON: {exc}') from exc


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
        return national.load_strict_json(str(path), label)
    except national.PublicationError as exc:
        raise ColoradoEvidenceError(str(exc)) from exc


def expect_keys(value, required, optional, label):
    if not isinstance(value, dict):
        raise ColoradoEvidenceError(f'{label} must be an object')
    missing = sorted(set(required) - set(value))
    extra = sorted(set(value) - set(required) - set(optional))
    if missing or extra:
        raise ColoradoEvidenceError(
            f'{label} keys mismatch: missing={missing}, extra={extra}')


def text(value, label, minimum=1, maximum=5000):
    if (not isinstance(value, str) or value != value.strip() or
            not minimum <= len(value) <= maximum or '\x00' in value):
        raise ColoradoEvidenceError(
            f'{label} must be trimmed text of length {minimum}..{maximum}')
    return value


def identifier(value, label):
    text(value, label, maximum=128)
    if ID_RE.fullmatch(value) is None:
        raise ColoradoEvidenceError(f'{label} must be a lowercase stable identifier')
    return value


def sha(value, label):
    if not isinstance(value, str) or SHA_RE.fullmatch(value) is None:
        raise ColoradoEvidenceError(f'{label} must be a lowercase SHA-256')
    return value


def positive_number(value, label):
    if (not isinstance(value, (int, float)) or isinstance(value, bool) or
            not math.isfinite(value) or value <= 0):
        raise ColoradoEvidenceError(f'{label} must be a positive finite number')


def is_inside(path, parent):
    try:
        return os.path.commonpath((str(path.resolve()), str(parent.resolve()))) == str(parent.resolve())
    except (OSError, ValueError):
        return False


def official_url(value, label):
    text(value, label, maximum=2048)
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme != 'https' or parsed.hostname not in OFFICIAL_HOSTS:
        raise ColoradoEvidenceError(
            f'{label} must use HTTPS on the approved official USGS host')
    if parsed.username or parsed.password or parsed.fragment:
        raise ColoradoEvidenceError(f'{label} contains forbidden URL components')


def validate_source_inventory(document):
    expect_keys(document, ('schema_version', 'dataset', 'sources'), (),
                'Colorado source inventory')
    if document['schema_version'] != 1:
        raise ColoradoEvidenceError('Colorado source inventory schema_version must be 1')
    if document['dataset'] != 'ws11-colorado-grade-source-inventory':
        raise ColoradoEvidenceError('Colorado source inventory dataset identity is invalid')
    rows = document['sources']
    if not isinstance(rows, list) or not rows:
        raise ColoradoEvidenceError('Colorado source inventory sources must be nonempty')
    sources = {}
    for index, row in enumerate(rows):
        label = f'Colorado source inventory.sources[{index}]'
        expect_keys(row, SOURCE_KEYS, (), label)
        source_id = identifier(row['source_id'], f'{label}.source_id')
        if source_id in sources:
            raise ColoradoEvidenceError(f'duplicate source_id {source_id}')
        for field in ('title', 'authority', 'citation'):
            text(row[field], f'{label}.{field}', minimum=3, maximum=1000)
        year = row['publication_year']
        if (not isinstance(year, int) or isinstance(year, bool) or
                not 1800 <= year <= 2100):
            raise ColoradoEvidenceError(f'{label}.publication_year is invalid')
        official_url(row['catalog_url'], f'{label}.catalog_url')
        official_url(row['document_url'], f'{label}.document_url')
        local = Path(text(row['local_path'], f'{label}.local_path', maximum=500))
        if local.is_absolute() or '..' in local.parts or '.' in local.parts:
            raise ColoradoEvidenceError(f'{label}.local_path must be normalized and relative')
        resolved = (ROOT / local).resolve()
        if not is_inside(resolved, ROOT / 'pipelines/cache/co-grade-sources'):
            raise ColoradoEvidenceError(
                f'{label}.local_path must stay in pipelines/cache/co-grade-sources')
        if not isinstance(row['bytes'], int) or isinstance(row['bytes'], bool) or row['bytes'] <= 0:
            raise ColoradoEvidenceError(f'{label}.bytes must be a positive integer')
        sha(row['sha256'], f'{label}.sha256')
        if not isinstance(row['pages'], int) or isinstance(row['pages'], bool) or row['pages'] <= 0:
            raise ColoradoEvidenceError(f'{label}.pages must be a positive integer')
        if row['text_mode'] != 'embedded':
            raise ColoradoEvidenceError(f'{label}.text_mode must be embedded')
        sources[source_id] = dict(row, resolved_path=resolved)
    if set(sources) != {'pp359', 'b478', 'pp610'}:
        raise ColoradoEvidenceError(
            'Colorado source inventory must contain exactly pp359, b478, and pp610')
    return sources


def validate_reviewed(document, sources):
    expect_keys(document, REVIEW_KEYS, (), 'reviewed Colorado grade evidence')
    if (document['schema_version'] != 1 or document['state'] != 'CO' or
            document['status'] != 'reviewed'):
        raise ColoradoEvidenceError(
            'reviewed Colorado evidence must be schema 1, state CO, status reviewed')
    for field in ('dataset', 'reviewed_on', 'reviewed_by', 'review_method'):
        text(document[field], f'reviewed Colorado evidence.{field}', minimum=3)
    mines = document['mines']
    if not isinstance(mines, list) or len(mines) < 25:
        raise ColoradoEvidenceError(
            f'reviewed Colorado evidence has {len(mines) if isinstance(mines, list) else 0} mines; at least 25 are required')
    mine_ids = set()
    evidence_ids = set()
    mine_names = set()
    used_sources = set()
    out = []
    for mine_index, mine in enumerate(mines):
        label = f'reviewed Colorado evidence.mines[{mine_index}]'
        expect_keys(mine, MINE_KEYS, (), label)
        mine_id = identifier(mine['mine_id'], f'{label}.mine_id')
        if mine_id in mine_ids:
            raise ColoradoEvidenceError(f'duplicate mine_id {mine_id}')
        mine_ids.add(mine_id)
        for field in ('name', 'district', 'county'):
            text(mine[field], f'{label}.{field}', minimum=2, maximum=300)
        name_key = (re.sub(r'[^a-z0-9]+', ' ', mine['name'].lower()).strip(),
                    re.sub(r'[^a-z0-9]+', ' ', mine['district'].lower()).strip())
        if name_key in mine_names:
            raise ColoradoEvidenceError(f'duplicate normalized mine/district {mine["name"]!r}')
        mine_names.add(name_key)
        evidence_rows = mine['evidence']
        if not isinstance(evidence_rows, list) or not evidence_rows:
            raise ColoradoEvidenceError(f'{label}.evidence must be nonempty')
        checked = []
        for ev_index, evidence in enumerate(evidence_rows):
            ev_label = f'{label}.evidence[{ev_index}]'
            expect_keys(evidence, EVIDENCE_REQUIRED, EVIDENCE_OPTIONAL, ev_label)
            evidence_id = identifier(evidence['evidence_id'], f'{ev_label}.evidence_id')
            if evidence_id in evidence_ids:
                raise ColoradoEvidenceError(f'duplicate evidence_id {evidence_id}')
            evidence_ids.add(evidence_id)
            source_id = identifier(evidence['source_id'], f'{ev_label}.source_id')
            if source_id not in sources or source_id == 'pp610':
                raise ColoradoEvidenceError(
                    f'{ev_label} references unknown/non-grade source {source_id}')
            used_sources.add(source_id)
            page = evidence['pdf_page']
            if (not isinstance(page, int) or isinstance(page, bool) or
                    not 1 <= page <= sources[source_id]['pages']):
                raise ColoradoEvidenceError(f'{ev_label}.pdf_page is outside the source')
            page_cite = text(evidence['page_cite'], f'{ev_label}.page_cite',
                             minimum=2, maximum=200)
            if not any(character.isdigit() for character in page_cite):
                raise ColoradoEvidenceError(f'{ev_label}.page_cite needs a page number')
            text(evidence['verbatim_quote'], f'{ev_label}.verbatim_quote', minimum=8)
            if evidence['quote_verbatim'] is not True:
                raise ColoradoEvidenceError(f'{ev_label}.quote_verbatim must be true')
            if source_id == 'pp359':
                if 'page_image_sha256' not in evidence:
                    raise ColoradoEvidenceError(
                        f'{ev_label} needs the reviewed PP 359 table-page image SHA-256')
                sha(evidence['page_image_sha256'], f'{ev_label}.page_image_sha256')
            elif 'page_image_sha256' in evidence:
                raise ColoradoEvidenceError(
                    f'{ev_label} must not add an image hash to narrative text evidence')
            measurements = evidence['measurements']
            if not isinstance(measurements, list) or not measurements:
                raise ColoradoEvidenceError(f'{ev_label}.measurements must be nonempty')
            commodities = set()
            for measurement_index, measurement in enumerate(measurements):
                measurement_label = f'{ev_label}.measurements[{measurement_index}]'
                expect_keys(measurement, ('commodity', 'value', 'unit'), (),
                            measurement_label)
                commodity = measurement['commodity']
                if commodity not in national.COMMODITIES or commodity in commodities:
                    raise ColoradoEvidenceError(
                        f'{measurement_label}.commodity is unsupported or duplicated')
                commodities.add(commodity)
                positive_number(measurement['value'], f'{measurement_label}.value')
                if measurement['unit'] not in national.NATIVE_UNITS[commodity]:
                    raise ColoradoEvidenceError(f'{measurement_label}.unit is invalid')
            for field in ('basis', 'years'):
                text(evidence[field], f'{ev_label}.{field}', maximum=500)
            checked.append(dict(evidence))
        out.append({**{key: mine[key] for key in ('mine_id', 'name', 'district', 'county')},
                    'evidence': checked})
    if used_sources != {'pp359', 'b478'}:
        raise ColoradoEvidenceError(
            'Colorado reviewed grades must use both pp359 and b478, without synthetic sources')
    return out, used_sources


def validate_district_inventory(document, sources):
    expect_keys(document,
                ('schema_version', 'dataset', 'state', 'source_id',
                 'review_scope', 'figure_page_image_sha256', 'districts'), (),
                'Colorado PP 610 inventory')
    if (document['schema_version'] != 1 or document['state'] != 'CO' or
            document['source_id'] != 'pp610' or
            document['dataset'] != 'ws11-colorado-pp610-district-inventory'):
        raise ColoradoEvidenceError('Colorado PP 610 inventory identity is invalid')
    text(document['review_scope'], 'Colorado PP 610 inventory.review_scope', minimum=40)
    sha(document['figure_page_image_sha256'],
        'Colorado PP 610 inventory.figure_page_image_sha256')
    rows = document['districts']
    if not isinstance(rows, list) or len(rows) != len(PP610_FIGURE_10):
        raise ColoradoEvidenceError(
            'Colorado PP 610 inventory must contain all 44 Figure 10 districts')
    ids = set()
    quotes = set()
    out = []
    for index, (row, expected, locator) in enumerate(zip(
            rows, PP610_FIGURE_10, PP610_DESCRIPTION_LOCATORS), 1):
        label = f'Colorado PP 610 inventory.districts[{index - 1}]'
        expect_keys(row, ('district_id', 'name', 'county', 'pdf_page',
                          'page_cite', 'source_heading', 'verbatim_quote',
                          'quote_verbatim'), (), label)
        district_id = identifier(row['district_id'], f'{label}.district_id')
        if district_id in ids:
            raise ColoradoEvidenceError(f'duplicate PP 610 district_id {district_id}')
        ids.add(district_id)
        if (row['name'], row['county']) != expected:
            raise ColoradoEvidenceError(
                f'{label} is not Figure 10 district {index}: expected {expected}')
        expected_page, expected_heading = locator
        if (row['pdf_page'] != expected_page or
                row['pdf_page'] > sources['pp610']['pages']):
            raise ColoradoEvidenceError(
                f'{label}.pdf_page must be reviewed description PDF page {expected_page}')
        if row['page_cite'] != f'p. {expected_page - 6}':
            raise ColoradoEvidenceError(
                f'{label}.page_cite must bind printed page {expected_page - 6}')
        if row['source_heading'] != expected_heading:
            raise ColoradoEvidenceError(
                f'{label}.source_heading differs from reviewed text-layer locator')
        quote = text(row['verbatim_quote'], f'{label}.verbatim_quote', minimum=8,
                     maximum=300)
        if row['quote_verbatim'] is not True:
            raise ColoradoEvidenceError(f'{label}.quote_verbatim must be true')
        if quote in quotes:
            raise ColoradoEvidenceError(f'{label} duplicates a Figure 10 quote')
        quotes.add(quote)
        out.append(dict(row))
    return out, document['figure_page_image_sha256']


def verify_pdf(source):
    path = source['resolved_path']
    if path.is_symlink():
        raise ColoradoEvidenceError(f'{source["source_id"]} PDF must not be a symlink')
    try:
        actual_bytes = path.stat().st_size
        actual_sha = sha256_file(path)
    except OSError as exc:
        raise ColoradoEvidenceError(
            f'{source["source_id"]} PDF unavailable; run fetch: {exc}') from exc
    if actual_bytes != source['bytes'] or actual_sha != source['sha256']:
        raise ColoradoEvidenceError(
            f'{source["source_id"]} source drift: expected '
            f'{source["bytes"]}/{source["sha256"]}, got {actual_bytes}/{actual_sha}')
    if not shutil.which('pdfinfo'):
        raise ColoradoEvidenceError('pdfinfo is required to verify source page counts')
    process = subprocess.run(['pdfinfo', str(path)], capture_output=True,
                             text=True, check=False)
    if process.returncode:
        raise ColoradoEvidenceError(
            f'pdfinfo rejected {source["source_id"]}: {process.stderr.strip()}')
    match = re.search(r'^Pages:\s+(\d+)\s*$', process.stdout, re.MULTILINE)
    if match is None or int(match.group(1)) != source['pages']:
        raise ColoradoEvidenceError(
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
        with urllib.request.urlopen(request, timeout=120) as response, open(temp_path, 'wb') as output:
            final = urllib.parse.urlparse(response.geturl())
            if final.scheme != 'https' or final.hostname not in OFFICIAL_HOSTS:
                raise ColoradoEvidenceError(
                    f'{source["source_id"]} redirected outside the official USGS host')
            remaining = source['bytes'] + 1
            while remaining:
                chunk = response.read(min(1 << 20, remaining))
                if not chunk:
                    break
                output.write(chunk)
                remaining -= len(chunk)
        if temp_path.stat().st_size != source['bytes'] or sha256_file(temp_path) != source['sha256']:
            raise ColoradoEvidenceError(
                f'{source["source_id"]} download does not match reviewed bytes/SHA-256')
        with open(temp_path, 'rb') as downloaded:
            if downloaded.read(5) != b'%PDF-':
                raise ColoradoEvidenceError(f'{source["source_id"]} response is not a PDF')
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink()
    verify_pdf(source)
    return 'downloaded'


def extract_text_page(source, pdf_page):
    if not shutil.which('pdftotext'):
        raise ColoradoEvidenceError('pdftotext is required for cited-page extraction')
    process = subprocess.run([
        'pdftotext', '-f', str(pdf_page), '-l', str(pdf_page), '-enc', 'UTF-8',
        '-layout', str(source['resolved_path']), '-'], capture_output=True,
        check=False)
    if process.returncode:
        raise ColoradoEvidenceError(
            f'pdftotext {source["source_id"]} page {pdf_page} failed: '
            f'{process.stderr.decode("utf-8", "replace").strip()}')
    return process.stdout


def render_page_image(source, pdf_page):
    if not shutil.which('pdftoppm'):
        raise ColoradoEvidenceError(
            'pdftoppm is required for PP 359 table-page review binding')
    with tempfile.TemporaryDirectory(prefix='co-grade-page-') as directory:
        base = Path(directory) / 'page'
        process = subprocess.run([
            'pdftoppm', '-f', str(pdf_page), '-l', str(pdf_page), '-r', '300',
            '-png', '-singlefile', str(source['resolved_path']), str(base)],
            capture_output=True, check=False)
        if process.returncode:
            raise ColoradoEvidenceError(
                f'pdftoppm {source["source_id"]} page {pdf_page} failed: '
                f'{process.stderr.decode("utf-8", "replace").strip()}')
        image = base.with_suffix('.png')
        if not image.is_file():
            raise ColoradoEvidenceError('pdftoppm did not create the reviewed page image')
        return sha256_file(image)


def page_record(source, page, cache, image_bound=False):
    key = (source['source_id'], page)
    if key not in cache:
        raw = extract_text_page(source, page)
        cache[key] = {
            'raw': raw,
            'text': raw.decode('utf-8', 'replace'),
            'page_text_sha256': sha256_bytes(raw),
            'page_image_sha256': None,
        }
    if image_bound and cache[key]['page_image_sha256'] is None:
        cache[key]['page_image_sha256'] = render_page_image(source, page)
    return cache[key]


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
        raise ColoradoEvidenceError(f'output path must not be a symlink: {path}')
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
    process = subprocess.run([command, '-v'], capture_output=True,
                             text=True, check=False)
    lines = (process.stdout or process.stderr).splitlines()
    return lines[0].strip() if lines else 'unknown'


def build(sources_path=DEFAULT_SOURCES, reviewed_path=DEFAULT_REVIEWED,
          districts_path=DEFAULT_DISTRICTS, output=DEFAULT_OUTPUT):
    sources_document, sources_raw = load_json(Path(sources_path), 'Colorado source inventory')
    reviewed_document, reviewed_raw = load_json(
        Path(reviewed_path), 'reviewed Colorado grade evidence')
    districts_document, districts_raw = load_json(
        Path(districts_path), 'Colorado PP 610 inventory')
    sources = validate_source_inventory(sources_document)
    mines, grade_source_ids = validate_reviewed(reviewed_document, sources)
    districts, pp_figure_image_sha = validate_district_inventory(
        districts_document, sources)
    for source_id in sorted(sources):
        verify_pdf(sources[source_id])

    output = Path(output).resolve()
    if is_inside(output, SITE):
        raise ColoradoEvidenceError('Colorado raw evidence output must stay outside site/')
    if output == ROOT or not is_inside(output, ROOT):
        raise ColoradoEvidenceError('Colorado raw evidence output must stay inside workspace')
    page_cache = {}
    source_page_rows = {source_id: {} for source_id in sources}

    grade_mines = []
    for mine in mines:
        out_mine = {key: mine[key] for key in ('mine_id', 'name', 'district', 'county')}
        out_mine['evidence'] = []
        for evidence in mine['evidence']:
            source = sources[evidence['source_id']]
            image_bound = evidence['source_id'] == 'pp359'
            page = page_record(source, evidence['pdf_page'], page_cache,
                               image_bound=image_bound)
            if image_bound and page['page_image_sha256'] != evidence['page_image_sha256']:
                raise ColoradoEvidenceError(
                    f'{evidence["evidence_id"]}: reviewed PP 359 table-page image SHA-256 changed')
            score = extraction.quote_match_score(
                evidence['verbatim_quote'], page['text'])
            if not image_bound and score < MIN_QUOTE_MATCH:
                raise ColoradoEvidenceError(
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
                    'page_image_sha256': page['page_image_sha256'],
                    'text_mode': 'embedded',
                    'checks': [],
                })
            page_row['checks'].append({
                'evidence_id': evidence['evidence_id'],
                'page_cite': evidence['page_cite'],
                'quote_match_score': score,
                'review_boundary': ('page_image_sha256_human_review_text_match_diagnostic'
                                    if image_bound else
                                    'page_text_sha256_and_quote_match'),
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
                'text_mode': 'embedded',
                'pdftotext_arguments': '-enc UTF-8 -layout',
                'quote_match_floor': MIN_QUOTE_MATCH,
                'table_page_render': ('pdftoppm -r 300 -png'
                                      if source_id == 'pp359' else None),
                'table_text_role': ('diagnostic_only_page_image_sha_is_review_boundary'
                                    if source_id == 'pp359' else None),
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
        'state': 'CO',
        'sources': [source_identities[source_id]
                    for source_id in sorted(source_identities)],
        'mines': grade_mines,
    }
    try:
        grade_validation = national.validate_grade_document(
            grades_document, 'CO', {}, '0' * 64)
    except national.PublicationError as exc:
        raise ColoradoEvidenceError(
            f'national grade contract rejected Colorado: {exc}') from exc
    grades_artifact = atomic_json(output / 'grades/co.json', grades_document)

    pp_source = sources['pp610']
    pp_page_rows = {}
    pp_districts = []
    pp_figure_page = page_record(pp_source, 91, page_cache, image_bound=True)
    if pp_figure_page['page_image_sha256'] != pp_figure_image_sha:
        raise ColoradoEvidenceError(
            'PP 610 Figure 10 reviewed page-image SHA-256 changed')
    bbox_cache = {}
    for district in districts:
        pdf_page = district['pdf_page']
        page = page_record(pp_source, pdf_page, page_cache)
        heading = district['source_heading']
        if heading is not None:
            if pdf_page not in bbox_cache:
                bbox_cache[pdf_page] = extraction.bbox_blocks(pp_source, pdf_page)
            derived = extraction.first_district_sentence(
                bbox_cache[pdf_page], heading, district['district_id'])
            if derived != district['verbatim_quote']:
                raise ColoradoEvidenceError(
                    f'{district["district_id"]}: reviewed PP 610 description changed')
            quote_mode = 'first_complete_sentence_after_exact_bbox_heading'
        else:
            quote_mode = 'reviewed_county_section_sentence_for_unheaded_figure_entry'
        score = extraction.quote_word_coverage(
            district['verbatim_quote'], page['text'])
        if score < 0.85:
            raise ColoradoEvidenceError(
                f'{district["district_id"]}: PP 610 quote/page word coverage '
                f'{score:.3f} is below 0.85')
        pp_districts.append({
            'district_id': district['district_id'],
            'name': district['name'],
            'page_cite': district['page_cite'],
            'verbatim_quote': district['verbatim_quote'],
            'quote_verbatim': True,
            'page_text_sha256': page['page_text_sha256'],
        })
        page_row = pp_page_rows.setdefault(pdf_page, {
            'pdf_page': pdf_page,
            'printed_page': pdf_page - 6,
            'page_text_sha256': page['page_text_sha256'],
            'checks': [],
        })
        page_row['checks'].append({
            'district_id': district['district_id'],
            'source_heading': heading,
            'quote_mode': quote_mode,
            'quote_word_coverage': score,
            'review_boundary': 'page_text_sha256_and_reviewed_bbox_or_county_quote',
        })
    pp_index = {
        'schema_version': 1,
        'source_id': 'pp610',
        'document_sha256': pp_source['sha256'],
        'extraction': {
            'text_mode': 'embedded',
            'pdftotext_arguments': '-enc UTF-8 -layout',
            'bbox_arguments': '-bbox-layout',
            'scope': ('complete Colorado Figure 10 inventory, with one reviewed '
                      'descriptive chapter quote per numbered district'),
            'figure_10_completeness_page': {
                'pdf_page': 91,
                'printed_page': 85,
                'page_text_sha256': pp_figure_page['page_text_sha256'],
                'page_image_sha256': pp_figure_page['page_image_sha256'],
                'page_render': 'pdftoppm -r 300 -png',
            },
        },
        'pages': [pp_page_rows[page] for page in sorted(pp_page_rows)],
    }
    pp_index_sha = sha256_bytes(canonical_bytes(pp_index))
    pp_index_path = output / 'page-indexes' / f'pp610.{pp_index_sha}.json'
    page_index_artifacts['pp610'] = atomic_json(pp_index_path, pp_index)
    pp_document = {
        'schema_version': 1,
        'state': 'CO',
        'complete': True,
        'source': source_identity(pp_source, pp_index_sha),
        'districts': pp_districts,
    }
    try:
        pp_validation = national.validate_pp610_document(pp_document, 'CO')
    except national.PublicationError as exc:
        raise ColoradoEvidenceError(
            f'national PP 610 contract rejected Colorado: {exc}') from exc
    pp_artifact = atomic_json(output / 'pp610/co.json', pp_document)

    relative = lambda path: str(Path(path).resolve().relative_to(output))
    for artifact in [grades_artifact, pp_artifact, *page_index_artifacts.values()]:
        artifact['path'] = relative(artifact['path'])
    report = {
        'schema_version': 1,
        'dataset': 'ws11-colorado-grade-evidence-build',
        'state': 'CO',
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
        } for source_id in sorted(sources)],
        'toolchain': {
            'python': sys.version.split()[0],
            'pdftotext': tool_version('pdftotext'),
            'pdfinfo': tool_version('pdfinfo'),
            'pdftoppm': tool_version('pdftoppm'),
        },
        'metrics': {
            **grade_validation['metrics'],
            'pp610_districts': pp_validation['district_count'],
            'figure_10_page_hashes': 1,
            'pp610_description_page_hashes': len(pp_page_rows),
            'pp359_table_image_pages_review_bound': sum(
                1 for (source_id, _), page in page_cache.items()
                if source_id == 'pp359' and page['page_image_sha256']),
        },
        'threshold_observation': {
            'at_least_25_graded_mines': grade_validation['metrics']['graded_mines'] >= 25,
            'at_least_2_primary_sources': grade_validation['metrics']['primary_sources'] >= 2,
            'complete_pp610_anchor': pp_validation['district_count'] == 44,
            'is_release_decision': False,
        },
        'artifacts': {
            'grade_input': grades_artifact,
            'pp610_input': pp_artifact,
            'page_indexes': page_index_artifacts,
        },
        'unconsumed_inventory_sources': [],
    }
    report_artifact = atomic_json(output / 'build.json', report)
    return report, report_artifact


def check_inputs(sources_path=DEFAULT_SOURCES, reviewed_path=DEFAULT_REVIEWED,
                 districts_path=DEFAULT_DISTRICTS):
    sources_document, _ = load_json(Path(sources_path), 'Colorado source inventory')
    reviewed_document, _ = load_json(
        Path(reviewed_path), 'reviewed Colorado grade evidence')
    districts_document, _ = load_json(
        Path(districts_path), 'Colorado PP 610 inventory')
    sources = validate_source_inventory(sources_document)
    mines, used = validate_reviewed(reviewed_document, sources)
    districts, _ = validate_district_inventory(districts_document, sources)
    return {'mines': len(mines), 'grade_sources': len(used),
            'pp610_districts': len(districts)}


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument('--sources', default=str(DEFAULT_SOURCES))
    result.add_argument('--reviewed', default=str(DEFAULT_REVIEWED))
    result.add_argument('--districts', default=str(DEFAULT_DISTRICTS))
    subparsers = result.add_subparsers(dest='command', required=True)
    subparsers.add_parser('check', help='validate review manifests without PDFs')
    fetch = subparsers.add_parser('fetch', help='fetch/verify official pinned PDFs')
    fetch.add_argument('--all', action='store_true',
                       help='accepted for parity; all three sources are consumed')
    build_parser = subparsers.add_parser(
        'build', help='verify pages and produce private compiler inputs')
    build_parser.add_argument('--output', default=str(DEFAULT_OUTPUT))
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command == 'check':
            result = check_inputs(args.sources, args.reviewed, args.districts)
        elif args.command == 'fetch':
            source_document, _ = load_json(Path(args.sources), 'Colorado source inventory')
            sources = validate_source_inventory(source_document)
            result = {source_id: fetch_source(sources[source_id])
                      for source_id in sorted(sources)}
        else:
            report, artifact = build(args.sources, args.reviewed, args.districts,
                                     args.output)
            result = {'metrics': report['metrics'], 'build': artifact}
    except (ColoradoEvidenceError, OSError) as exc:
        print(f'Colorado grade evidence ERROR: {exc}', file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
