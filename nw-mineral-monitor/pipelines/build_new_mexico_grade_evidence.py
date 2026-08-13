#!/usr/bin/env python3
"""Build checksum-bound New Mexico WS11 grade and PP 610 evidence inputs.

This producer is intentionally private and New-Mexico-only. It verifies
official USGS documents, binds every reviewed scan quotation to a deterministic
300-dpi page render, and emits inputs accepted by
``build_national_grade_evidence.py``. It never writes below ``site/`` and never
changes a registry, coverage gate, manifest, DONE flag, or release flag.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.parse
from pathlib import Path

import build_colorado_grade_evidence as shared
import build_national_grade_evidence as national


HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SITE = ROOT / 'site'
DEFAULT_SOURCES = ROOT / 'pipelines/config/nm_grade_sources.json'
DEFAULT_REVIEWED = ROOT / 'grades-research/nm/reviewed_grade_evidence.json'
DEFAULT_DISTRICTS = ROOT / 'grades-research/nm/pp610_district_inventory.json'
DEFAULT_OUTPUT = ROOT / 'build-inputs/ws9/nm-grade-evidence'
OFFICIAL_HOSTS = frozenset(('pubs.usgs.gov',))
EXPECTED_SOURCE_IDS = frozenset(('pp68', 'b870', 'pp610'))
EXPECTED_GRADE_SOURCE_IDS = frozenset(('pp68', 'b870'))
EXPECTED_MINE_IDS = frozenset((
    'nm-victor-no-2-claim', 'nm-ivanhoe-grafton', 'nm-mahoning-group',
    'nm-nana-mine', 'nm-new-era-mine', 'nm-wicks-mine',
    'nm-bonanza-hillsboro', 'nm-thirty-stope', 'nm-emporia-incline',
    'nm-bunkhouse-workings', 'nm-jessie-chance-ore-body',
    'nm-cleveland-group', 'nm-apache-no-2-mine',
    'nm-international-mine', 'nm-eagle-mine-fremont',
    'nm-daisy-mine-fremont', 'nm-silver-fox-mine',
    'nm-american-property', 'nm-barnett-property', 'nm-doyle-property',
    'nm-lucky-bill-mine', 'nm-ground-hog-mine',
    'nm-three-brothers-mine', 'nm-owl-mine', 'nm-silver-king-mine',
    'nm-lion-no-2-mine'))
PP610_FIGURE_PAGE = 209
PP610_FIGURE_PRINTED_PAGE = 203
PP610_DISTRICTS = (
    ('nm-tijeras-canyon', 'Tijeras Canyon', 'Bernalillo', 208,
     'TIJE.RAS CANYON DISTRICT'),
    ('nm-mogollon', 'Mogollon', 'Catron', 208, 'MOGOLLON DISTRICT'),
    ('nm-elizabethtown-baldy', 'Elizabethtown-Baldy', 'Colfax', 210,
     'ELIZABETHTOWN -BALDY DISTRICT'),
    ('nm-organ', 'Organ', 'Dona Ana', 210, 'ORGAN DISTRICT'),
    ('nm-central', 'Central', 'Grant', 211, 'CENTRAL DISTRICT'),
    ('nm-pinos-altos', 'Pinos Altos', 'Grant', 212,
     'PINOS ALTOS DISTRICT'),
    ('nm-steeple-rock', 'Steeple Rock', 'Grant', 212,
     'STEEPLE ROCK DISTRICT'),
    ('nm-lordsburg', 'Lordsburg', 'Hidalgo', 213, 'LORDSBURG DISTRICT'),
    ('nm-white-oaks', 'White Oaks', 'Lincoln', 214,
     'WHITE OAKS DISTRICT'),
    ('nm-nogal', 'Nogal', 'Lincoln', 213, 'NOGAL DISTRICT'),
    ('nm-jarilla', 'Jarilla', 'Otero', 214, 'JARILLA DISTRICT'),
    ('nm-cochiti', 'Cochiti', 'Sandoval', 214, 'COCHITI DISTRICT'),
    ('nm-willow-creek', 'Willow Creek', 'San Miguel', 215,
     'WILLOW CREEK DISTRICT'),
    ('nm-old-placer', 'Old Placer', 'Santa Fe', 216,
     'OLD PLACER DISTRICT'),
    ('nm-new-placer', 'New Placer', 'Santa Fe', 215,
     'NEW PLACER DISTRICT'),
    ('nm-hillsboro', 'Hillsboro', 'Sierra', 216, 'HILLSBORO DISTRICT'),
    ('nm-rosedale', 'Rosedale', 'Socorro', 217, 'ROSEDALE DISTRICT'),
)
SOURCE_KEYS = frozenset((
    'source_id', 'title', 'authority', 'citation', 'publication_year',
    'catalog_url', 'document_url', 'local_path', 'bytes', 'sha256', 'pages',
    'text_mode'))
REVIEW_KEYS = frozenset((
    'schema_version', 'dataset', 'state', 'status', 'reviewed_on',
    'reviewed_by', 'review_method', 'mines'))
MINE_KEYS = frozenset(('mine_id', 'name', 'district', 'county', 'evidence'))
EVIDENCE_KEYS = frozenset((
    'evidence_id', 'source_id', 'pdf_page', 'page_image_sha256', 'page_cite',
    'verbatim_quote', 'quote_verbatim', 'measurements', 'basis', 'years'))

NewMexicoEvidenceError = shared.ColoradoEvidenceError
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
fetch_source = shared.fetch_source
page_record = shared.page_record
source_identity = shared.source_identity
atomic_json = shared.atomic_json
tool_version = shared.tool_version


def official_url(value, label):
    """Require a plain HTTPS URL on the pinned official USGS host."""
    text(value, label, maximum=2048)
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme != 'https' or parsed.hostname not in OFFICIAL_HOSTS:
        raise NewMexicoEvidenceError(
            f'{label} must use HTTPS on the approved official USGS host')
    if parsed.username or parsed.password or parsed.fragment:
        raise NewMexicoEvidenceError(f'{label} contains forbidden URL components')
    return value


def validate_source_inventory(document):
    expect_keys(document, ('schema_version', 'dataset', 'sources'), (),
                'New Mexico source inventory')
    if (document['schema_version'] != 1 or
            document['dataset'] != 'ws11-new-mexico-grade-source-inventory'):
        raise NewMexicoEvidenceError(
            'New Mexico source inventory identity/schema is invalid')
    if not isinstance(document['sources'], list):
        raise NewMexicoEvidenceError('New Mexico sources must be a list')
    sources = {}
    for index, row in enumerate(document['sources']):
        label = f'New Mexico source inventory.sources[{index}]'
        expect_keys(row, SOURCE_KEYS, (), label)
        source_id = identifier(row['source_id'], f'{label}.source_id')
        if source_id in sources:
            raise NewMexicoEvidenceError(f'duplicate source_id {source_id}')
        for field in ('title', 'authority', 'citation'):
            text(row[field], f'{label}.{field}', minimum=3, maximum=1000)
        if (not isinstance(row['publication_year'], int) or
                isinstance(row['publication_year'], bool) or
                not 1800 <= row['publication_year'] <= 2100):
            raise NewMexicoEvidenceError(
                f'{label}.publication_year is invalid')
        official_url(row['catalog_url'], f'{label}.catalog_url')
        official_url(row['document_url'], f'{label}.document_url')
        local = Path(text(row['local_path'], f'{label}.local_path', maximum=500))
        if local.is_absolute() or '..' in local.parts or '.' in local.parts:
            raise NewMexicoEvidenceError(
                f'{label}.local_path must be a normalized relative path')
        resolved = (ROOT / local).resolve()
        if not is_inside(resolved, ROOT / 'pipelines/cache/nm-grade-sources'):
            raise NewMexicoEvidenceError(
                f'{label}.local_path must stay in pipelines/cache/nm-grade-sources')
        if (not isinstance(row['bytes'], int) or isinstance(row['bytes'], bool)
                or row['bytes'] <= 0):
            raise NewMexicoEvidenceError(f'{label}.bytes must be positive')
        sha(row['sha256'], f'{label}.sha256')
        if (not isinstance(row['pages'], int) or isinstance(row['pages'], bool)
                or row['pages'] <= 0):
            raise NewMexicoEvidenceError(f'{label}.pages must be positive')
        expected_mode = 'embedded' if source_id == 'pp610' else 'embedded_scan'
        if row['text_mode'] != expected_mode:
            raise NewMexicoEvidenceError(
                f'{label}.text_mode must be {expected_mode}')
        sources[source_id] = dict(row, resolved_path=resolved)
    if set(sources) != EXPECTED_SOURCE_IDS:
        raise NewMexicoEvidenceError(
            'New Mexico source inventory must contain exactly pp68, b870, '
            'and pp610')
    return sources


def validate_reviewed(document, sources):
    expect_keys(document, REVIEW_KEYS, (), 'reviewed New Mexico grade evidence')
    if (document['schema_version'] != 1 or document['state'] != 'NM' or
            document['status'] != 'reviewed' or
            document['dataset'] !=
            'ws11-new-mexico-reviewed-grade-extraction'):
        raise NewMexicoEvidenceError(
            'reviewed New Mexico evidence identity/status is invalid')
    for field in ('reviewed_on', 'reviewed_by', 'review_method'):
        text(document[field], f'reviewed New Mexico evidence.{field}', minimum=3)
    rows = document['mines']
    if not isinstance(rows, list) or len(rows) < 25:
        raise NewMexicoEvidenceError(
            f'reviewed New Mexico evidence has '
            f'{len(rows) if isinstance(rows, list) else 0} mines; '
            'at least 25 are required')
    mines = []
    mine_ids = set()
    evidence_ids = set()
    normalized_names = set()
    used_sources = set()
    for mine_index, mine in enumerate(rows):
        label = f'reviewed New Mexico evidence.mines[{mine_index}]'
        expect_keys(mine, MINE_KEYS, (), label)
        mine_id = identifier(mine['mine_id'], f'{label}.mine_id')
        if mine_id in mine_ids:
            raise NewMexicoEvidenceError(f'duplicate mine_id {mine_id}')
        mine_ids.add(mine_id)
        for field in ('name', 'district', 'county'):
            text(mine[field], f'{label}.{field}', minimum=2, maximum=300)
        name_key = (
            re.sub(r'[^a-z0-9]+', ' ', mine['name'].lower()).strip(),
            re.sub(r'[^a-z0-9]+', ' ', mine['district'].lower()).strip())
        if name_key in normalized_names:
            raise NewMexicoEvidenceError(
                f'duplicate normalized mine/district {mine["name"]!r}')
        normalized_names.add(name_key)
        if not isinstance(mine['evidence'], list) or not mine['evidence']:
            raise NewMexicoEvidenceError(f'{label}.evidence must be nonempty')
        checked = []
        for evidence_index, evidence in enumerate(mine['evidence']):
            ev_label = f'{label}.evidence[{evidence_index}]'
            expect_keys(evidence, EVIDENCE_KEYS, (), ev_label)
            evidence_id = identifier(
                evidence['evidence_id'], f'{ev_label}.evidence_id')
            if evidence_id in evidence_ids:
                raise NewMexicoEvidenceError(
                    f'duplicate evidence_id {evidence_id}')
            evidence_ids.add(evidence_id)
            source_id = identifier(
                evidence['source_id'], f'{ev_label}.source_id')
            if source_id not in EXPECTED_GRADE_SOURCE_IDS:
                raise NewMexicoEvidenceError(
                    f'{ev_label} references an unknown/non-grade source')
            used_sources.add(source_id)
            source = sources[source_id]
            page = evidence['pdf_page']
            if (not isinstance(page, int) or isinstance(page, bool) or
                    not 1 <= page <= source['pages']):
                raise NewMexicoEvidenceError(
                    f'{ev_label}.pdf_page is outside the source')
            page_cite = text(
                evidence['page_cite'], f'{ev_label}.page_cite', minimum=2,
                maximum=200)
            if not any(character.isdigit() for character in page_cite):
                raise NewMexicoEvidenceError(
                    f'{ev_label}.page_cite must contain a page number')
            sha(evidence['page_image_sha256'],
                f'{ev_label}.page_image_sha256')
            text(evidence['verbatim_quote'], f'{ev_label}.verbatim_quote',
                 minimum=8)
            if evidence['quote_verbatim'] is not True:
                raise NewMexicoEvidenceError(
                    f'{ev_label}.quote_verbatim must be true')
            measurements = evidence['measurements']
            if not isinstance(measurements, list) or not measurements:
                raise NewMexicoEvidenceError(
                    f'{ev_label}.measurements must be nonempty')
            commodities = set()
            for measurement_index, measurement in enumerate(measurements):
                measurement_label = (
                    f'{ev_label}.measurements[{measurement_index}]')
                expect_keys(measurement, ('commodity', 'value', 'unit'), (),
                            measurement_label)
                commodity = measurement['commodity']
                if (commodity not in national.COMMODITIES or
                        commodity in commodities):
                    raise NewMexicoEvidenceError(
                        f'{measurement_label}.commodity is unsupported or duplicated')
                commodities.add(commodity)
                positive_number(measurement['value'],
                                f'{measurement_label}.value')
                if measurement['unit'] not in national.NATIVE_UNITS[commodity]:
                    raise NewMexicoEvidenceError(
                        f'{measurement_label}.unit is invalid')
            for field in ('basis', 'years'):
                text(evidence[field], f'{ev_label}.{field}', maximum=500)
            checked.append(dict(evidence))
        mines.append({
            **{key: mine[key]
               for key in ('mine_id', 'name', 'district', 'county')},
            'evidence': checked})
    if mine_ids != EXPECTED_MINE_IDS:
        raise NewMexicoEvidenceError(
            'New Mexico reviewed grade target identity set is incomplete')
    if used_sources != EXPECTED_GRADE_SOURCE_IDS:
        raise NewMexicoEvidenceError(
            'New Mexico reviewed grades must consume both pp68 and b870')
    return mines, used_sources


def validate_district_inventory(document, sources):
    expect_keys(document, (
        'schema_version', 'dataset', 'state', 'source_id', 'review_scope',
        'figure_page_image_sha256', 'districts'), (),
        'New Mexico PP 610 inventory')
    if (document['schema_version'] != 1 or document['state'] != 'NM' or
            document['source_id'] != 'pp610' or
            document['dataset'] !=
            'ws11-new-mexico-pp610-district-inventory'):
        raise NewMexicoEvidenceError(
            'New Mexico PP 610 inventory identity is invalid')
    text(document['review_scope'],
         'New Mexico PP 610 inventory.review_scope', minimum=40)
    figure_sha = sha(document['figure_page_image_sha256'],
                     'New Mexico PP 610 figure page SHA-256')
    rows = document['districts']
    if not isinstance(rows, list) or len(rows) != len(PP610_DISTRICTS):
        raise NewMexicoEvidenceError(
            'New Mexico PP 610 inventory must contain all 17 Figure 19 districts')
    result = []
    for index, (row, expected) in enumerate(zip(rows, PP610_DISTRICTS)):
        label = f'New Mexico PP 610 inventory.districts[{index}]'
        expect_keys(row, ('district_id', 'name', 'county', 'pdf_page',
                          'source_heading'), (), label)
        actual = (identifier(row['district_id'], f'{label}.district_id'),
                  row['name'], row['county'], row['pdf_page'],
                  row['source_heading'])
        if actual != expected:
            raise NewMexicoEvidenceError(
                f'{label} differs from reviewed Figure 19/chapter inventory')
        if row['pdf_page'] > sources['pp610']['pages']:
            raise NewMexicoEvidenceError(f'{label}.pdf_page is outside PP 610')
        for field in ('name', 'county', 'source_heading'):
            text(row[field], f'{label}.{field}', minimum=2, maximum=300)
        result.append(dict(row))
    return result, figure_sha


def build(sources_path=DEFAULT_SOURCES, reviewed_path=DEFAULT_REVIEWED,
          districts_path=DEFAULT_DISTRICTS, output=DEFAULT_OUTPUT):
    sources_document, sources_raw = load_json(
        Path(sources_path), 'New Mexico source inventory')
    reviewed_document, reviewed_raw = load_json(
        Path(reviewed_path), 'reviewed New Mexico grade evidence')
    districts_document, districts_raw = load_json(
        Path(districts_path), 'New Mexico PP 610 district inventory')
    sources = validate_source_inventory(sources_document)
    mines, grade_source_ids = validate_reviewed(reviewed_document, sources)
    districts, figure_image_sha = validate_district_inventory(
        districts_document, sources)
    for source_id in sorted(sources):
        verify_pdf(sources[source_id])

    output = Path(output).resolve()
    if is_inside(output, SITE):
        raise NewMexicoEvidenceError(
            'New Mexico raw evidence output must stay outside site/')
    if output == ROOT or not is_inside(output, ROOT):
        raise NewMexicoEvidenceError(
            'New Mexico raw evidence output must stay inside the workspace')

    page_cache = {}
    source_page_rows = {source_id: {} for source_id in sources}
    grade_mines = []
    for mine in mines:
        out_mine = {
            key: mine[key] for key in ('mine_id', 'name', 'district', 'county')}
        out_mine['evidence'] = []
        for evidence in mine['evidence']:
            source = sources[evidence['source_id']]
            page = page_record(
                source, evidence['pdf_page'], page_cache, image_bound=True)
            if page['page_image_sha256'] != evidence['page_image_sha256']:
                raise NewMexicoEvidenceError(
                    f'{evidence["evidence_id"]}: reviewed scan page-image '
                    'SHA-256 changed')
            score = shared.extraction.quote_match_score(
                evidence['verbatim_quote'], page['text'])
            out_mine['evidence'].append({
                'evidence_id': evidence['evidence_id'],
                'source_id': evidence['source_id'],
                'page_cite': evidence['page_cite'],
                'verbatim_quote': evidence['verbatim_quote'],
                'quote_verbatim': True,
                'page_text_sha256': page['page_text_sha256'],
                'measurements': evidence['measurements'],
                'basis': evidence['basis'],
                'years': evidence['years'],
            })
            page_row = source_page_rows[evidence['source_id']].setdefault(
                evidence['pdf_page'], {
                    'pdf_page': evidence['pdf_page'],
                    'printed_page': int(re.search(
                        r'(\d+)', evidence['page_cite']).group(1)),
                    'page_text_sha256': page['page_text_sha256'],
                    'page_image_sha256': page['page_image_sha256'],
                    'text_mode': 'embedded_scan',
                    'checks': [],
                })
            page_row['checks'].append({
                'evidence_id': evidence['evidence_id'],
                'page_cite': evidence['page_cite'],
                'quote_match_score': score,
                'review_boundary': 'page_image_sha256_human_review',
                'embedded_ocr_role': 'diagnostic_only',
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
                'text_mode': 'embedded_scan',
                'pdftotext_arguments': '-enc UTF-8 -layout',
                'page_render': 'pdftoppm -r 300 -png',
                'review_boundary': 'page_image_sha256_human_review',
                'embedded_ocr_role': 'diagnostic_search_cross_check_only',
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
        'state': 'NM',
        'sources': [source_identities[source_id]
                    for source_id in sorted(source_identities)],
        'mines': grade_mines,
    }
    try:
        grade_validation = national.validate_grade_document(
            grades_document, 'NM', {}, '0' * 64)
    except national.PublicationError as exc:
        raise NewMexicoEvidenceError(
            f'national grade contract rejected New Mexico: {exc}') from exc
    grades_artifact = atomic_json(output / 'grades/nm.json', grades_document)

    pp_source = sources['pp610']
    figure_page = page_record(
        pp_source, PP610_FIGURE_PAGE, page_cache, image_bound=True)
    if figure_page['page_image_sha256'] != figure_image_sha:
        raise NewMexicoEvidenceError(
            'PP 610 Figure 19 reviewed page-image SHA-256 changed')
    pp_page_rows = {}
    pp_districts = []
    bbox_cache = {}
    for district in districts:
        pdf_page = district['pdf_page']
        page = page_record(pp_source, pdf_page, page_cache)
        if pdf_page not in bbox_cache:
            bbox_cache[pdf_page] = shared.extraction.bbox_blocks(
                pp_source, pdf_page)
        quote = shared.extraction.first_district_sentence(
            bbox_cache[pdf_page], district['source_heading'],
            district['district_id'])
        score = shared.extraction.quote_word_coverage(quote, page['text'])
        if score < 0.85:
            raise NewMexicoEvidenceError(
                f'{district["district_id"]}: PP 610 quote/page word coverage '
                f'{score:.3f} is below 0.85')
        pp_districts.append({
            'district_id': district['district_id'],
            'name': district['name'],
            'page_cite': f'p. {pdf_page - 6}',
            'verbatim_quote': quote,
            'quote_verbatim': True,
            'page_text_sha256': page['page_text_sha256'],
        })
        pp_page_rows.setdefault(pdf_page, {
            'pdf_page': pdf_page,
            'printed_page': pdf_page - 6,
            'page_text_sha256': page['page_text_sha256'],
            'checks': [],
        })['checks'].append({
            'district_id': district['district_id'],
            'source_heading': district['source_heading'],
            'quote_match_score': score,
            'review_boundary':
                'page_text_sha256_and_exact_bbox_heading_derivation',
        })
    pp_index = {
        'schema_version': 1,
        'source_id': 'pp610',
        'document_sha256': pp_source['sha256'],
        'reviewed_scope': {
            'figure': 'Figure 19',
            'district_count': len(PP610_DISTRICTS),
            'chapter_pdf_pages': '208-217',
            'chapter_printed_pages': '202-211',
        },
        'extraction': {
            'page_text': 'pdftotext -enc UTF-8 -layout',
            'bbox': 'pdftotext -bbox-layout',
            'quote': 'first complete sentence after exact bbox heading',
            'figure_19_completeness_page': {
                'pdf_page': PP610_FIGURE_PAGE,
                'printed_page': PP610_FIGURE_PRINTED_PAGE,
                'page_text_sha256': figure_page['page_text_sha256'],
                'page_image_sha256': figure_page['page_image_sha256'],
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
        'state': 'NM',
        'complete': True,
        'source': source_identity(pp_source, pp_index_sha),
        'districts': pp_districts,
    }
    try:
        pp_validation = national.validate_pp610_document(pp_document, 'NM')
    except national.PublicationError as exc:
        raise NewMexicoEvidenceError(
            f'national PP 610 contract rejected New Mexico: {exc}') from exc
    pp_artifact = atomic_json(output / 'pp610/nm.json', pp_document)

    def relative(path):
        return str(Path(path).resolve().relative_to(output))

    for artifact in [grades_artifact, pp_artifact,
                     *page_index_artifacts.values()]:
        artifact['path'] = relative(artifact['path'])
    report = {
        'schema_version': 1,
        'dataset': 'ws11-new-mexico-grade-evidence-build',
        'state': 'NM',
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
            'scan_image_pages_review_bound': sum(
                1 for (source_id, _), page in page_cache.items()
                if source_id in EXPECTED_GRADE_SOURCE_IDS and
                page['page_image_sha256']),
            'pp610_figure_image_pages_review_bound': 1,
        },
        'threshold_observation': {
            'at_least_25_graded_mines':
                grade_validation['metrics']['graded_mines'] >= 25,
            'at_least_2_primary_sources':
                grade_validation['metrics']['primary_sources'] >= 2,
            'complete_pp610_anchor':
                pp_validation['district_count'] == len(PP610_DISTRICTS),
            'is_release_decision': False,
        },
        'artifacts': {
            'grade_input': grades_artifact,
            'pp610_input': pp_artifact,
            'page_indexes': page_index_artifacts,
        },
    }
    report_artifact = atomic_json(output / 'build.json', report)
    return report, report_artifact


def check_inputs(sources_path=DEFAULT_SOURCES, reviewed_path=DEFAULT_REVIEWED,
                 districts_path=DEFAULT_DISTRICTS):
    sources_document, _ = load_json(
        Path(sources_path), 'New Mexico source inventory')
    reviewed_document, _ = load_json(
        Path(reviewed_path), 'reviewed New Mexico grade evidence')
    districts_document, _ = load_json(
        Path(districts_path), 'New Mexico PP 610 district inventory')
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
    subparsers.add_parser(
        'check', help='validate reviewed manifests without the PDF cache')
    subparsers.add_parser(
        'fetch', help='fetch or verify the three checksum-pinned USGS PDFs')
    build_parser = subparsers.add_parser(
        'build', help='verify source pages and produce private compiler inputs')
    build_parser.add_argument('--output', default=str(DEFAULT_OUTPUT))
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command == 'check':
            result = check_inputs(args.sources, args.reviewed, args.districts)
        elif args.command == 'fetch':
            source_document, _ = load_json(
                Path(args.sources), 'New Mexico source inventory')
            sources = validate_source_inventory(source_document)
            result = {source_id: fetch_source(sources[source_id])
                      for source_id in sorted(sources)}
        else:
            report, artifact = build(
                args.sources, args.reviewed, args.districts, args.output)
            result = {'metrics': report['metrics'], 'build': artifact}
    except (NewMexicoEvidenceError, OSError) as exc:
        print(f'New Mexico grade evidence ERROR: {exc}', file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
