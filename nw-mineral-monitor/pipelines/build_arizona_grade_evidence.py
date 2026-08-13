#!/usr/bin/env python3
"""Build checksum-bound Arizona WS9 grade and PP 610 evidence inputs.

This is an Arizona-only private evidence producer. It verifies official
AZGS-hosted and USGS PDFs, binds every reviewed quotation to the cited page,
and writes inputs accepted by ``build_national_grade_evidence.py``. It never
writes below ``site/`` and never changes a state registry, DONE flag,
manifest, coverage document, or release.

The historic Arizona grade publications are scans. Their deterministic
300-dpi page-render SHA-256 is the human-review boundary; OCR is only a
diagnostic cross-check. PP 610 uses its official text layer, with ordered
bounding-box extraction for district quotations.
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
import urllib.parse
import urllib.request
from pathlib import Path

import build_national_grade_evidence as national
import build_nevada_grade_evidence as shared


HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SITE = ROOT / 'site'
DEFAULT_SOURCES = ROOT / 'pipelines/config/az_grade_sources.json'
DEFAULT_REVIEWED = ROOT / 'grades-research/az/reviewed_grade_evidence.json'
DEFAULT_DISTRICTS = ROOT / 'grades-research/az/pp610_district_inventory.json'
DEFAULT_OUTPUT = ROOT / 'build-inputs/ws9/az-grade-evidence'
OFFICIAL_HOSTS = frozenset((
    'data.azgs.arizona.edu', 'library.azgs.arizona.edu', 'pubs.usgs.gov'))
EXPECTED_SOURCE_IDS = frozenset((
    'azbm-b137', 'usbm-ic6991', 'usgs-b782', 'pp610'))
EXPECTED_GRADE_SOURCE_IDS = EXPECTED_SOURCE_IDS - {'pp610'}
EXPECTED_DISTRICT_IDS = frozenset((
    'az-bisbee', 'az-dos-cabezas', 'az-tombstone', 'az-turquoise',
    'az-banner', 'az-globe-miami', 'az-ash-peak', 'az-clifton-morenci',
    'az-cave-creek', 'az-vulture', 'az-gold-basin', 'az-san-francisco',
    'az-wallapai', 'az-weaver-mohave', 'az-ajo', 'az-greaterville',
    'az-mammoth', 'az-ray', 'az-superior', 'az-oro-blanco',
    'az-agua-fria', 'az-big-bug', 'az-black-canyon', 'az-black-rock',
    'az-eureka', 'az-hassayampa-groom-creek', 'az-jerome',
    'az-lynx-creek-walker', 'az-martinez', 'az-peck',
    'az-pine-grove-tiger', 'az-tiptop', 'az-weaver-rich-hill',
    'az-castle-dome', 'az-cienega', 'az-dome', 'az-ellsworth',
    'az-fortuna', 'az-kofa', 'az-laguna', 'az-la-paz', 'az-plomosa'))
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

# Reuse the already-reviewed deterministic JSON, PDF, text-normalization, and
# bounding-box primitives from the Nevada producer. Arizona owns all source,
# schema, identity, completeness, page-binding, and output decisions below.
ArizonaEvidenceError = shared.NevadaEvidenceError
canonical_bytes = shared.canonical_bytes
sha256_bytes = shared.sha256_bytes
sha256_file = shared.sha256_file
load_json = shared.load_json
expect_keys = shared.expect_keys
text = shared.text
identifier = shared.identifier
sha = shared.sha
positive_number = shared.positive_number
is_inside = shared.is_inside
verify_pdf = shared.verify_pdf
run_tool = shared.run_tool
extract_text_page = shared.extract_text_page
quote_match_score = shared.quote_match_score
quote_word_coverage = shared.quote_word_coverage
bbox_blocks = shared.bbox_blocks
first_district_sentence = shared.first_district_sentence
source_identity = shared.source_identity
atomic_json = shared.atomic_json
tool_version = shared.tool_version


def official_url(value, label):
    """Require an HTTPS URL on one of the pinned Arizona source hosts."""
    text(value, label, maximum=2048)
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme != 'https' or parsed.hostname not in OFFICIAL_HOSTS:
        raise ArizonaEvidenceError(
            f'{label} must use HTTPS on an approved official AZGS/USGS host')
    if parsed.username or parsed.password or parsed.fragment:
        raise ArizonaEvidenceError(f'{label} contains forbidden URL components')
    return value


def validate_source_inventory(document):
    expect_keys(document, ('schema_version', 'dataset', 'sources'), (),
                'Arizona source inventory')
    if document['schema_version'] != 1:
        raise ArizonaEvidenceError(
            'Arizona source inventory schema_version must be 1')
    if document['dataset'] != 'ws11-arizona-grade-source-inventory':
        raise ArizonaEvidenceError(
            'Arizona source inventory dataset identity is invalid')
    if not isinstance(document['sources'], list) or not document['sources']:
        raise ArizonaEvidenceError(
            'Arizona source inventory sources must be nonempty')
    sources = {}
    for index, row in enumerate(document['sources']):
        label = f'Arizona source inventory.sources[{index}]'
        expect_keys(row, SOURCE_KEYS, (), label)
        source_id = identifier(row['source_id'], f'{label}.source_id')
        if source_id in sources:
            raise ArizonaEvidenceError(f'duplicate source_id {source_id}')
        for field in ('title', 'authority', 'citation'):
            text(row[field], f'{label}.{field}', minimum=3, maximum=1000)
        year = row['publication_year']
        if (not isinstance(year, int) or isinstance(year, bool) or
                not 1800 <= year <= 2100):
            raise ArizonaEvidenceError(f'{label}.publication_year is invalid')
        official_url(row['catalog_url'], f'{label}.catalog_url')
        official_url(row['document_url'], f'{label}.document_url')
        local = Path(text(row['local_path'], f'{label}.local_path', maximum=500))
        if local.is_absolute() or '..' in local.parts or '.' in local.parts:
            raise ArizonaEvidenceError(
                f'{label}.local_path must be a normalized relative path')
        resolved = (ROOT / local).resolve()
        if not is_inside(resolved, ROOT / 'pipelines/cache/az-grade-sources'):
            raise ArizonaEvidenceError(
                f'{label}.local_path must stay in pipelines/cache/az-grade-sources')
        if (not isinstance(row['bytes'], int) or isinstance(row['bytes'], bool)
                or row['bytes'] <= 0):
            raise ArizonaEvidenceError(
                f'{label}.bytes must be a positive integer')
        sha(row['sha256'], f'{label}.sha256')
        if (not isinstance(row['pages'], int) or isinstance(row['pages'], bool)
                or row['pages'] <= 0):
            raise ArizonaEvidenceError(
                f'{label}.pages must be a positive integer')
        if row['text_mode'] not in ('embedded', 'ocr'):
            raise ArizonaEvidenceError(
                f'{label}.text_mode must be embedded or ocr')
        sources[source_id] = dict(row, resolved_path=resolved)
    if set(sources) != EXPECTED_SOURCE_IDS:
        raise ArizonaEvidenceError(
            'Arizona source inventory must contain exactly the four reviewed '
            f'sources; got {sorted(sources)}')
    if sources['pp610']['text_mode'] != 'embedded':
        raise ArizonaEvidenceError('PP 610 must use its official PDF text layer')
    if any(sources[source_id]['text_mode'] != 'ocr'
           for source_id in EXPECTED_GRADE_SOURCE_IDS):
        raise ArizonaEvidenceError(
            'Arizona historic grade scans must use page-image review mode')
    return sources


def validate_reviewed(document, sources):
    expect_keys(document, REVIEW_KEYS, (), 'reviewed Arizona grade evidence')
    if (document['schema_version'] != 1 or document['state'] != 'AZ' or
            document['status'] != 'reviewed' or
            document['dataset'] != 'ws11-arizona-reviewed-grade-extraction'):
        raise ArizonaEvidenceError(
            'reviewed Arizona evidence identity/status is invalid')
    for field in ('reviewed_on', 'reviewed_by', 'review_method'):
        text(document[field], f'reviewed Arizona evidence.{field}', minimum=3)
    mines = document['mines']
    if not isinstance(mines, list) or len(mines) < 25:
        raise ArizonaEvidenceError(
            f'reviewed Arizona evidence has '
            f'{len(mines) if isinstance(mines, list) else 0} mines; '
            'at least 25 are required')
    mine_ids = set()
    evidence_ids = set()
    mine_names = set()
    used_sources = set()
    out = []
    for mine_index, mine in enumerate(mines):
        label = f'reviewed Arizona evidence.mines[{mine_index}]'
        expect_keys(mine, MINE_KEYS, (), label)
        mine_id = identifier(mine['mine_id'], f'{label}.mine_id')
        if mine_id in mine_ids:
            raise ArizonaEvidenceError(f'duplicate mine_id {mine_id}')
        mine_ids.add(mine_id)
        for field in ('name', 'district', 'county'):
            text(mine[field], f'{label}.{field}', minimum=2, maximum=300)
        name_key = (
            re.sub(r'[^a-z0-9]+', ' ', mine['name'].lower()).strip(),
            re.sub(r'[^a-z0-9]+', ' ', mine['district'].lower()).strip())
        if name_key in mine_names:
            raise ArizonaEvidenceError(
                f'duplicate normalized mine/district {mine["name"]!r}')
        mine_names.add(name_key)
        if not isinstance(mine['evidence'], list) or not mine['evidence']:
            raise ArizonaEvidenceError(f'{label}.evidence must be nonempty')
        evidence_out = []
        for evidence_index, evidence in enumerate(mine['evidence']):
            ev_label = f'{label}.evidence[{evidence_index}]'
            expect_keys(evidence, EVIDENCE_REQUIRED, EVIDENCE_OPTIONAL, ev_label)
            evidence_id = identifier(
                evidence['evidence_id'], f'{ev_label}.evidence_id')
            if evidence_id in evidence_ids:
                raise ArizonaEvidenceError(
                    f'duplicate evidence_id {evidence_id}')
            evidence_ids.add(evidence_id)
            source_id = identifier(evidence['source_id'], f'{ev_label}.source_id')
            if source_id not in sources or source_id == 'pp610':
                raise ArizonaEvidenceError(
                    f'{ev_label} references unknown/non-grade source {source_id}')
            used_sources.add(source_id)
            source = sources[source_id]
            page = evidence['pdf_page']
            if (not isinstance(page, int) or isinstance(page, bool) or
                    not 1 <= page <= source['pages']):
                raise ArizonaEvidenceError(
                    f'{ev_label}.pdf_page is outside the source')
            page_cite = text(
                evidence['page_cite'], f'{ev_label}.page_cite', minimum=2,
                maximum=200)
            if not any(character.isdigit() for character in page_cite):
                raise ArizonaEvidenceError(
                    f'{ev_label}.page_cite must contain a page number')
            text(evidence['verbatim_quote'],
                 f'{ev_label}.verbatim_quote', minimum=8)
            if evidence['quote_verbatim'] is not True:
                raise ArizonaEvidenceError(
                    f'{ev_label}.quote_verbatim must be true')
            measurements = evidence['measurements']
            if not isinstance(measurements, list) or not measurements:
                raise ArizonaEvidenceError(
                    f'{ev_label} must contain at least one native measurement')
            seen_commodities = set()
            for measurement_index, measurement in enumerate(measurements):
                measurement_label = (
                    f'{ev_label}.measurements[{measurement_index}]')
                expect_keys(measurement, ('commodity', 'value', 'unit'), (),
                            measurement_label)
                commodity = measurement['commodity']
                if (commodity not in national.COMMODITIES or
                        commodity in seen_commodities):
                    raise ArizonaEvidenceError(
                        f'{measurement_label}.commodity is unsupported or duplicated')
                seen_commodities.add(commodity)
                positive_number(measurement['value'],
                                f'{measurement_label}.value')
                if measurement['unit'] not in national.NATIVE_UNITS[commodity]:
                    raise ArizonaEvidenceError(
                        f'{measurement_label}.unit is invalid')
            for field in ('basis', 'years'):
                text(evidence[field], f'{ev_label}.{field}', maximum=500)
            if source['text_mode'] == 'ocr':
                if 'page_image_sha256' not in evidence:
                    raise ArizonaEvidenceError(
                        f'{ev_label} needs the reviewed scan page SHA-256')
                sha(evidence['page_image_sha256'],
                    f'{ev_label}.page_image_sha256')
            elif 'page_image_sha256' in evidence:
                raise ArizonaEvidenceError(
                    f'{ev_label} must not add an image hash to a text-layer source')
            evidence_out.append(dict(evidence))
        mine_out = {
            key: mine[key] for key in ('mine_id', 'name', 'district', 'county')}
        mine_out['evidence'] = evidence_out
        out.append(mine_out)
    if used_sources != EXPECTED_GRADE_SOURCE_IDS:
        raise ArizonaEvidenceError(
            'Arizona reviewed grades must use all three independent reviewed '
            f'primary grade sources; got {sorted(used_sources)}')
    return out, used_sources


def validate_district_inventory(document, sources):
    expect_keys(document,
                ('schema_version', 'dataset', 'state', 'source_id',
                 'review_scope', 'districts'), (), 'Arizona PP 610 inventory')
    if (document['schema_version'] != 1 or document['state'] != 'AZ' or
            document['source_id'] != 'pp610' or
            document['dataset'] != 'ws11-arizona-pp610-district-inventory'):
        raise ArizonaEvidenceError('Arizona PP 610 inventory identity is invalid')
    text(document['review_scope'],
         'Arizona PP 610 inventory.review_scope', minimum=8)
    rows = document['districts']
    if not isinstance(rows, list) or len(rows) != 42:
        raise ArizonaEvidenceError(
            'Arizona PP 610 inventory must contain all 42 Figure 7 districts')
    ids = set()
    names = set()
    expected_counties = {
        'Cochise', 'Gila', 'Greenlee', 'Maricopa', 'Mohave', 'Pima',
        'Pinal', 'Santa Cruz', 'Yavapai', 'Yuma'}
    out = []
    source = sources['pp610']
    for index, row in enumerate(rows):
        label = f'Arizona PP 610 inventory.districts[{index}]'
        expect_keys(row, ('district_id', 'name', 'county', 'pdf_page',
                          'source_heading'), (), label)
        district_id = identifier(row['district_id'], f'{label}.district_id')
        if district_id in ids:
            raise ArizonaEvidenceError(
                f'duplicate PP 610 district_id {district_id}')
        ids.add(district_id)
        name = text(row['name'], f'{label}.name', minimum=2, maximum=300)
        name_key = re.sub(r'[^a-z0-9]+', ' ', name.lower()).strip()
        if name_key in names:
            raise ArizonaEvidenceError(
                f'duplicate PP 610 district name {name!r}')
        names.add(name_key)
        if row['county'] not in expected_counties:
            raise ArizonaEvidenceError(
                f'{label}.county is outside reviewed Arizona scope')
        page = row['pdf_page']
        if (not isinstance(page, int) or isinstance(page, bool) or
                not 41 <= page <= 59 or page > source['pages']):
            raise ArizonaEvidenceError(
                f'{label}.pdf_page is outside the Arizona chapter')
        text(row['source_heading'], f'{label}.source_heading', minimum=8,
             maximum=200)
        out.append(dict(row))
    if ids != EXPECTED_DISTRICT_IDS:
        raise ArizonaEvidenceError(
            'Arizona PP 610 inventory district identity set is incomplete')
    return out


def fetch_source(source):
    """Fetch one pinned source, rejecting drift and cross-host redirects."""
    path = source['resolved_path']
    if path.exists():
        verify_pdf(source)
        return 'verified'
    path.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(
        source['document_url'],
        headers={'User-Agent': 'nw-mineral-monitor-ws11/1'})
    temp_path = path.with_name(path.name + '.part')
    try:
        with urllib.request.urlopen(request, timeout=90) as response, \
                open(temp_path, 'wb') as output:
            final = urllib.parse.urlparse(response.geturl())
            if final.scheme != 'https' or final.hostname not in OFFICIAL_HOSTS:
                raise ArizonaEvidenceError(
                    f'{source["source_id"]} redirected outside approved official hosts')
            remaining = source['bytes'] + 1
            while remaining:
                chunk = response.read(min(1 << 20, remaining))
                if not chunk:
                    break
                output.write(chunk)
                remaining -= len(chunk)
        if (temp_path.stat().st_size != source['bytes'] or
                sha256_file(temp_path) != source['sha256']):
            raise ArizonaEvidenceError(
                f'{source["source_id"]} download does not match reviewed '
                'bytes/SHA-256')
        with open(temp_path, 'rb') as downloaded:
            magic = downloaded.read(5)
        if magic != b'%PDF-':
            raise ArizonaEvidenceError(
                f'{source["source_id"]} response is not a PDF')
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink()
    verify_pdf(source)
    return 'downloaded'


def extract_ocr_page(source, pdf_page):
    """Render a scan deterministically and return diagnostic OCR plus PNG hash."""
    for tool in ('pdftoppm', 'tesseract'):
        if not shutil.which(tool):
            raise ArizonaEvidenceError(
                f'{tool} is required for scanned Arizona source pages')
    with tempfile.TemporaryDirectory(prefix='az-grade-page-') as directory:
        base = Path(directory) / 'page'
        run_tool([
            'pdftoppm', '-f', str(pdf_page), '-l', str(pdf_page), '-r', '300',
            '-png', '-singlefile', str(source['resolved_path']), str(base)],
            f'pdftoppm {source["source_id"]} page {pdf_page}')
        image = base.with_suffix('.png')
        if not image.is_file():
            raise ArizonaEvidenceError(
                'pdftoppm did not create the reviewed page image')
        image_sha = sha256_file(image)
        process = subprocess.run(
            ['tesseract', image.name, 'stdout', '--psm', '6'], cwd=directory,
            capture_output=True, check=False)
        if process.returncode:
            raise ArizonaEvidenceError(
                f'tesseract {source["source_id"]} page {pdf_page} failed: '
                f'{process.stderr.decode("utf-8", "replace").strip()}')
        return process.stdout, image_sha


def page_record(source, page, page_cache):
    cache_key = (source['source_id'], page)
    if cache_key not in page_cache:
        if source['text_mode'] == 'ocr':
            raw, image_sha = extract_ocr_page(source, page)
        else:
            raw = extract_text_page(source, page)
            image_sha = None
        page_cache[cache_key] = {
            'raw': raw,
            'text': raw.decode('utf-8', 'replace'),
            'page_text_sha256': sha256_bytes(raw),
            'page_image_sha256': image_sha,
        }
    return page_cache[cache_key]


def build(sources_path=DEFAULT_SOURCES, reviewed_path=DEFAULT_REVIEWED,
          districts_path=DEFAULT_DISTRICTS, output=DEFAULT_OUTPUT):
    sources_document, sources_raw = load_json(
        Path(sources_path), 'Arizona source inventory')
    reviewed_document, reviewed_raw = load_json(
        Path(reviewed_path), 'reviewed Arizona grade evidence')
    districts_document, districts_raw = load_json(
        Path(districts_path), 'Arizona PP 610 inventory')
    sources = validate_source_inventory(sources_document)
    mines, grade_source_ids = validate_reviewed(reviewed_document, sources)
    districts = validate_district_inventory(districts_document, sources)
    required_source_ids = set(grade_source_ids) | {'pp610'}
    for source_id in sorted(required_source_ids):
        verify_pdf(sources[source_id])

    output = Path(output).resolve()
    if is_inside(output, SITE):
        raise ArizonaEvidenceError(
            'Arizona raw evidence output must stay outside site/')
    if output == ROOT or not is_inside(output, ROOT):
        raise ArizonaEvidenceError(
            'Arizona raw evidence output must stay inside the workspace')

    page_cache = {}
    source_page_rows = {source_id: {} for source_id in required_source_ids}
    grade_mines = []
    for mine in mines:
        out_mine = {
            key: mine[key] for key in ('mine_id', 'name', 'district', 'county')}
        out_mine['evidence'] = []
        for evidence in mine['evidence']:
            source = sources[evidence['source_id']]
            page = page_record(source, evidence['pdf_page'], page_cache)
            if page['page_image_sha256'] != evidence['page_image_sha256']:
                raise ArizonaEvidenceError(
                    f'{evidence["evidence_id"]}: reviewed Arizona scan '
                    'page-image SHA-256 changed')
            score = quote_match_score(
                evidence['verbatim_quote'], page['text'])
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
                'review_boundary': 'page_image_sha256',
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
                'text_mode': 'ocr',
                'ocr_arguments': 'pdftoppm -r 300 -png; tesseract --psm 6',
                'ocr_role': (
                    'search_cross_check_only_page_image_sha_is_review_boundary'),
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
        'state': 'AZ',
        'sources': [source_identities[source_id]
                    for source_id in sorted(source_identities)],
        'mines': grade_mines,
    }
    try:
        grade_validation = national.validate_grade_document(
            grades_document, 'AZ', {}, '0' * 64)
    except national.PublicationError as exc:
        raise ArizonaEvidenceError(
            f'national grade contract rejected Arizona: {exc}') from exc
    grades_artifact = atomic_json(output / 'grades/az.json', grades_document)

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
            bbox_cache[pdf_page], district['source_heading'],
            district['district_id'])
        score = quote_word_coverage(quote, page['text'])
        if score < 0.85:
            raise ArizonaEvidenceError(
                f'{district["district_id"]}: derived PP 610 quote/page '
                f'match {score:.3f}')
        pp_districts.append({
            'district_id': district['district_id'],
            'name': district['name'],
            'page_cite': f'p. {pdf_page - 6}',
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
        'reviewed_scope': {
            'pdf_pages': '41-59', 'printed_pages': '35-53'},
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
        'state': 'AZ',
        'complete': True,
        'source': source_identity(pp_source, pp_index_sha),
        'districts': pp_districts,
    }
    try:
        pp_validation = national.validate_pp610_document(pp_document, 'AZ')
    except national.PublicationError as exc:
        raise ArizonaEvidenceError(
            f'national PP 610 contract rejected Arizona: {exc}') from exc
    pp_artifact = atomic_json(output / 'pp610/az.json', pp_document)

    relative = lambda path: str(Path(path).resolve().relative_to(output))
    for artifact in [grades_artifact, pp_artifact,
                     *page_index_artifacts.values()]:
        artifact['path'] = relative(artifact['path'])
    report = {
        'schema_version': 1,
        'dataset': 'ws11-arizona-grade-evidence-build',
        'state': 'AZ',
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
            'scan_image_pages_review_bound': sum(
                1 for page in page_cache.values()
                if page['page_image_sha256']),
        },
        'threshold_observation': {
            'at_least_25_graded_mines': (
                grade_validation['metrics']['graded_mines'] >= 25),
            'at_least_2_primary_sources': (
                grade_validation['metrics']['primary_sources'] >= 2),
            'complete_pp610_anchor': pp_validation['district_count'] == 42,
            'is_release_decision': False,
        },
        'artifacts': {
            'grade_input': grades_artifact,
            'pp610_input': pp_artifact,
            'page_indexes': page_index_artifacts,
        },
        'unconsumed_inventory_sources': sorted(
            set(sources) - required_source_ids),
    }
    report_artifact = atomic_json(output / 'build.json', report)
    return report, report_artifact


def check_inputs(sources_path=DEFAULT_SOURCES, reviewed_path=DEFAULT_REVIEWED,
                 districts_path=DEFAULT_DISTRICTS):
    sources_document, _ = load_json(
        Path(sources_path), 'Arizona source inventory')
    reviewed_document, _ = load_json(
        Path(reviewed_path), 'reviewed Arizona grade evidence')
    districts_document, _ = load_json(
        Path(districts_path), 'Arizona PP 610 inventory')
    sources = validate_source_inventory(sources_document)
    mines, used = validate_reviewed(reviewed_document, sources)
    districts = validate_district_inventory(districts_document, sources)
    return {
        'mines': len(mines), 'grade_sources': len(used),
        'pp610_districts': len(districts)}


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument('--sources', default=str(DEFAULT_SOURCES))
    result.add_argument('--reviewed', default=str(DEFAULT_REVIEWED))
    result.add_argument('--districts', default=str(DEFAULT_DISTRICTS))
    subparsers = result.add_subparsers(dest='command', required=True)
    subparsers.add_parser(
        'check', help='validate review manifests without source PDFs')
    fetch = subparsers.add_parser(
        'fetch', help='fetch/verify official pinned PDFs')
    fetch.add_argument(
        '--all', action='store_true',
        help='also fetch inventory candidates unused by this build')
    build_parser = subparsers.add_parser(
        'build', help='verify pages and produce private inputs')
    build_parser.add_argument('--output', default=str(DEFAULT_OUTPUT))
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command == 'check':
            result = check_inputs(args.sources, args.reviewed, args.districts)
        elif args.command == 'fetch':
            sources_document, _ = load_json(
                Path(args.sources), 'Arizona source inventory')
            reviewed_document, _ = load_json(
                Path(args.reviewed), 'reviewed Arizona grade evidence')
            districts_document, _ = load_json(
                Path(args.districts), 'Arizona PP 610 inventory')
            sources = validate_source_inventory(sources_document)
            _, used = validate_reviewed(reviewed_document, sources)
            validate_district_inventory(districts_document, sources)
            source_ids = set(sources) if args.all else set(used) | {'pp610'}
            result = {
                source_id: fetch_source(sources[source_id])
                for source_id in sorted(source_ids)}
        else:
            report, artifact = build(
                args.sources, args.reviewed, args.districts, args.output)
            result = {'metrics': report['metrics'], 'build': artifact}
    except (ArizonaEvidenceError, OSError) as exc:
        print(f'Arizona grade evidence ERROR: {exc}', file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
