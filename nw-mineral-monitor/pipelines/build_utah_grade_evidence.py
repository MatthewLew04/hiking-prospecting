#!/usr/bin/env python3
"""Build checksum-bound Utah WS11 grade and PP 610 evidence inputs.

This Utah-only producer verifies official USGS scans, binds every reviewed
quotation to an exact PDF page and deterministic 300-dpi page render, and emits
private inputs accepted by ``build_national_grade_evidence.py``.  It never writes
below ``site/`` and never changes registry, manifest, coverage, DONE, or release
state.

The checked extraction contains 26 named Utah mines from three independent USGS
monographs.  PP 610 completeness is the entire 13-entry numbered legend in
Figure 25, rather than a sample of familiar districts.  OCR is only a diagnostic
cross-check; the page-render hash is the review boundary for every historic scan.
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
DEFAULT_SOURCES = ROOT / 'pipelines/config/ut_grade_sources.json'
DEFAULT_REVIEWED = ROOT / 'grades-research/ut/reviewed_grade_evidence.json'
DEFAULT_DISTRICTS = ROOT / 'grades-research/ut/pp610_district_inventory.json'
DEFAULT_OUTPUT = ROOT / 'build-inputs/ws9/ut-grade-evidence'
OFFICIAL_HOSTS = frozenset(('pubs.usgs.gov',))
EXPECTED_SOURCE_IDS = frozenset(('pp38', 'pp107', 'pp177', 'pp610'))
EXPECTED_GRADE_SOURCE_IDS = EXPECTED_SOURCE_IDS - {'pp610'}
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
    'quote_verbatim', 'page_image_sha256', 'measurements', 'basis', 'years'))
MIN_QUOTE_MATCH = 0.55

EXPECTED_MINE_IDS = frozenset((
    'ut-old-jordan-mine', 'ut-neptune-mine', 'ut-old-telegraph-mine',
    'ut-columbia-mine', 'ut-highland-boy-mine', 'ut-phoenix-mine',
    'ut-montezuma-mine', 'ut-hoogley-mine', 'ut-vespasian-mine',
    'ut-navajo-tunnels', 'ut-julia-dean-mine',
    'ut-red-wing-extension-mine', 'ut-winamuck-mine',
    'ut-tiewaukee-mine', 'ut-midland-mine', 'ut-silver-bell-mine',
    'ut-congor-mine', 'ut-gemini-mine', 'ut-victoria-mine',
    'ut-colorado-mine', 'ut-eureka-hill-mine',
    'ut-centennial-eureka-mine', 'ut-mammoth-mine', 'ut-rube-mine',
    'ut-new-baltimore-mine', 'ut-monocco-claim'))

# Exact source/PDF/printed-page bindings prevent the inaccurate page and mine
# attributions in the earlier broad NV/UT triage file from entering evidence.
GRADE_LOCATORS = {
    'ut-old-jordan-lions-den-assay': ('pp38', 271, 'p. 239'),
    'ut-neptune-two-year-shipments': ('pp38', 275, 'p. 243'),
    'ut-old-telegraph-lead-carbonate-ore': ('pp38', 284, 'p. 252'),
    'ut-columbia-main-vein-assays': ('pp38', 293, 'p. 261'),
    'ut-highland-boy-development-shipments': ('pp38', 298, 'p. 265'),
    'ut-phoenix-1900-marketed-ore': ('pp38', 308, 'p. 274'),
    'ut-montezuma-reported-average': ('pp38', 325, 'p. 291'),
    'ut-hoogley-number-two-level': ('pp38', 326, 'p. 292'),
    'ut-vespasian-zinc-rich-ore': ('pp38', 327, 'p. 293'),
    'ut-navajo-small-lens': ('pp38', 329, 'p. 295'),
    'ut-julia-dean-main-tunnel-streak': ('pp38', 330, 'p. 296'),
    'ut-red-wing-extension-upper-tunnel': ('pp38', 333, 'p. 299'),
    'ut-winamuck-1872-smelted-ore': ('pp38', 337, 'p. 303'),
    'ut-tiewaukee-1880-extraction': ('pp38', 340, 'p. 306'),
    'ut-midland-northern-shoot': ('pp38', 343, 'p. 309'),
    'ut-silver-bell-overlying-quartzite-ore': ('pp38', 343, 'p. 309'),
    'ut-congor-northeast-fissure': ('pp38', 348, 'p. 314'),
    'ut-gemini-enargite-specimen': ('pp107', 181, 'p. 160'),
    'ut-victoria-1050-level-rich-ore': ('pp107', 203, 'p. 174'),
    'ut-colorado-ore-average': ('pp107', 203, 'p. 174'),
    'ut-eureka-hill-oxidized-copper-shipment': ('pp107', 203, 'p. 174'),
    'ut-centennial-eureka-gem-channel': ('pp107', 235, 'p. 201'),
    'ut-mammoth-production-average': ('pp107', 248, 'p. 214'),
    'ut-rube-shipments-1921-1927': ('pp177', 152, 'p. 136'),
    'ut-new-baltimore-1923-shipment': ('pp177', 166, 'p. 149'),
    'ut-monocco-1917-1920-shipments': ('pp177', 183, 'p. 162'),
}

PP610_FIGURE_25 = (
    ('ut-san-francisco', 'San Francisco', 'Beaver', '1, San Francisco.'),
    ('ut-stateline', 'Stateline', 'Iron', '2, Stateline.'),
    ('ut-tintic', 'Tintic', 'Juab', '3, Tintic.'),
    ('ut-gold-mountain', 'Gold Mountain', 'Piute', '4, Gold Mountain;'),
    ('ut-mount-baldy', 'Mount Baldy', 'Piute', '5, Mount Baldy.'),
    ('ut-cottonwood', 'Cottonwood', 'Salt Lake', '6, Cottonwood;'),
    ('ut-bingham', 'Bingham', 'Salt Lake', '7, Bingham.'),
    ('ut-park-city', 'Park City', 'Summit and Wasatch', '8, Park City.'),
    ('ut-camp-floyd', 'Camp Floyd', 'Tooele', '9, Camp Floyd;'),
    ('ut-ophir-rush-valley', 'Ophir-Rush Valley', 'Tooele',
     '10, Ophir-Rush Valley;'),
    ('ut-clifton', 'Clifton', 'Tooele', '11, Clifton;'),
    ('ut-willow-springs', 'Willow Springs', 'Tooele',
     '12, Willow Springs.'),
    ('ut-american-fork', 'American Fork', 'Utah', '13, American Fork.'),
)

# Reuse only state-neutral extraction and canonicalization primitives.  Utah
# owns all identity, completeness, page binding, and output decisions below.
UtahEvidenceError = shared.NevadaEvidenceError
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
extract_ocr_page = shared.extract_ocr_page
quote_match_score = shared.quote_match_score
quote_word_coverage = shared.quote_word_coverage
source_identity = shared.source_identity
atomic_json = shared.atomic_json
tool_version = shared.tool_version


def official_url(value, label):
    text(value, label, maximum=2048)
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme != 'https' or parsed.hostname not in OFFICIAL_HOSTS:
        raise UtahEvidenceError(
            f'{label} must use HTTPS on the approved official USGS host')
    if parsed.username or parsed.password or parsed.fragment:
        raise UtahEvidenceError(f'{label} contains forbidden URL components')
    return value


def validate_source_inventory(document):
    expect_keys(document, ('schema_version', 'dataset', 'sources'), (),
                'Utah source inventory')
    if (document['schema_version'] != 1 or
            document['dataset'] != 'ws11-utah-grade-source-inventory'):
        raise UtahEvidenceError('Utah source inventory identity is invalid')
    rows = document['sources']
    if not isinstance(rows, list) or not rows:
        raise UtahEvidenceError('Utah source inventory sources must be nonempty')
    sources = {}
    for index, row in enumerate(rows):
        label = f'Utah source inventory.sources[{index}]'
        expect_keys(row, SOURCE_KEYS, (), label)
        source_id = identifier(row['source_id'], f'{label}.source_id')
        if source_id in sources:
            raise UtahEvidenceError(f'duplicate source_id {source_id}')
        for field in ('title', 'authority', 'citation'):
            text(row[field], f'{label}.{field}', minimum=3, maximum=1000)
        year = row['publication_year']
        if (not isinstance(year, int) or isinstance(year, bool) or
                not 1800 <= year <= 2100):
            raise UtahEvidenceError(f'{label}.publication_year is invalid')
        official_url(row['catalog_url'], f'{label}.catalog_url')
        official_url(row['document_url'], f'{label}.document_url')
        local = Path(text(row['local_path'], f'{label}.local_path', maximum=500))
        if local.is_absolute() or '..' in local.parts or '.' in local.parts:
            raise UtahEvidenceError(
                f'{label}.local_path must be a normalized relative path')
        resolved = (ROOT / local).resolve()
        if not is_inside(resolved, ROOT / 'pipelines/cache/ut-grade-sources'):
            raise UtahEvidenceError(
                f'{label}.local_path must stay in pipelines/cache/ut-grade-sources')
        if (not isinstance(row['bytes'], int) or isinstance(row['bytes'], bool)
                or row['bytes'] <= 0):
            raise UtahEvidenceError(f'{label}.bytes must be a positive integer')
        sha(row['sha256'], f'{label}.sha256')
        if (not isinstance(row['pages'], int) or isinstance(row['pages'], bool)
                or row['pages'] <= 0):
            raise UtahEvidenceError(f'{label}.pages must be a positive integer')
        if row['text_mode'] not in ('embedded', 'ocr'):
            raise UtahEvidenceError(f'{label}.text_mode is invalid')
        sources[source_id] = dict(row, resolved_path=resolved)
    if set(sources) != EXPECTED_SOURCE_IDS:
        raise UtahEvidenceError(
            'Utah source inventory must contain exactly pp38, pp107, pp177, '
            'and pp610')
    if sources['pp610']['text_mode'] != 'embedded':
        raise UtahEvidenceError('PP 610 must use its official PDF text layer')
    if any(sources[source_id]['text_mode'] != 'ocr'
           for source_id in EXPECTED_GRADE_SOURCE_IDS):
        raise UtahEvidenceError(
            'Utah historic grade scans must use page-image review mode')
    return sources


def validate_reviewed(document, sources):
    expect_keys(document, REVIEW_KEYS, (), 'reviewed Utah grade evidence')
    if (document['schema_version'] != 1 or document['state'] != 'UT' or
            document['status'] != 'reviewed' or
            document['dataset'] != 'ws11-utah-reviewed-grade-extraction'):
        raise UtahEvidenceError('reviewed Utah evidence identity/status is invalid')
    for field in ('reviewed_on', 'reviewed_by', 'review_method'):
        text(document[field], f'reviewed Utah evidence.{field}', minimum=3)
    mines = document['mines']
    if not isinstance(mines, list) or len(mines) != 26:
        raise UtahEvidenceError(
            'reviewed Utah evidence must contain the 26 reviewed named mines')
    mine_ids = set()
    mine_names = set()
    evidence_ids = set()
    used_sources = set()
    out = []
    for mine_index, mine in enumerate(mines):
        label = f'reviewed Utah evidence.mines[{mine_index}]'
        expect_keys(mine, MINE_KEYS, (), label)
        mine_id = identifier(mine['mine_id'], f'{label}.mine_id')
        if mine_id in mine_ids:
            raise UtahEvidenceError(f'duplicate mine_id {mine_id}')
        mine_ids.add(mine_id)
        for field in ('name', 'district', 'county'):
            text(mine[field], f'{label}.{field}', minimum=2, maximum=300)
        name_key = (
            re.sub(r'[^a-z0-9]+', ' ', mine['name'].lower()).strip(),
            re.sub(r'[^a-z0-9]+', ' ', mine['district'].lower()).strip())
        if name_key in mine_names:
            raise UtahEvidenceError(
                f'duplicate normalized mine/district {mine["name"]!r}')
        mine_names.add(name_key)
        evidence_rows = mine['evidence']
        if not isinstance(evidence_rows, list) or len(evidence_rows) != 1:
            raise UtahEvidenceError(
                f'{label}.evidence must contain the one reviewed quotation')
        evidence = evidence_rows[0]
        ev_label = f'{label}.evidence[0]'
        expect_keys(evidence, EVIDENCE_REQUIRED, (), ev_label)
        evidence_id = identifier(evidence['evidence_id'],
                                 f'{ev_label}.evidence_id')
        if evidence_id in evidence_ids:
            raise UtahEvidenceError(f'duplicate evidence_id {evidence_id}')
        evidence_ids.add(evidence_id)
        expected_locator = GRADE_LOCATORS.get(evidence_id)
        actual_locator = (evidence['source_id'], evidence['pdf_page'],
                          evidence['page_cite'])
        if expected_locator != actual_locator:
            raise UtahEvidenceError(
                f'{evidence_id}: reviewed source/PDF/printed-page binding changed')
        source_id = identifier(evidence['source_id'], f'{ev_label}.source_id')
        if source_id not in EXPECTED_GRADE_SOURCE_IDS:
            raise UtahEvidenceError(f'{ev_label} references a non-grade source')
        used_sources.add(source_id)
        if evidence['pdf_page'] > sources[source_id]['pages']:
            raise UtahEvidenceError(f'{ev_label}.pdf_page is outside the source')
        text(evidence['page_cite'], f'{ev_label}.page_cite', minimum=2,
             maximum=200)
        sha(evidence['page_image_sha256'],
            f'{ev_label}.page_image_sha256')
        text(evidence['verbatim_quote'], f'{ev_label}.verbatim_quote', minimum=8)
        if evidence['quote_verbatim'] is not True:
            raise UtahEvidenceError(f'{ev_label}.quote_verbatim must be true')
        measurements = evidence['measurements']
        if not isinstance(measurements, list) or not measurements:
            raise UtahEvidenceError(f'{ev_label}.measurements must be nonempty')
        commodities = set()
        for measurement_index, measurement in enumerate(measurements):
            measurement_label = f'{ev_label}.measurements[{measurement_index}]'
            expect_keys(measurement, ('commodity', 'value', 'unit'), (),
                        measurement_label)
            commodity = measurement['commodity']
            if commodity not in national.COMMODITIES or commodity in commodities:
                raise UtahEvidenceError(
                    f'{measurement_label}.commodity is unsupported or duplicated')
            commodities.add(commodity)
            positive_number(measurement['value'], f'{measurement_label}.value')
            if measurement['unit'] not in national.NATIVE_UNITS[commodity]:
                raise UtahEvidenceError(f'{measurement_label}.unit is invalid')
        for field in ('basis', 'years'):
            text(evidence[field], f'{ev_label}.{field}', maximum=500)
        out.append({
            **{key: mine[key] for key in ('mine_id', 'name', 'district', 'county')},
            'evidence': [dict(evidence)],
        })
    if mine_ids != EXPECTED_MINE_IDS or evidence_ids != set(GRADE_LOCATORS):
        raise UtahEvidenceError('Utah reviewed mine/evidence identity set changed')
    if used_sources != EXPECTED_GRADE_SOURCE_IDS:
        raise UtahEvidenceError(
            'Utah reviewed grades must use pp38, pp107, and pp177')
    return out, used_sources


def validate_district_inventory(document, sources):
    expect_keys(document,
                ('schema_version', 'dataset', 'state', 'source_id',
                 'review_scope', 'figure_page_image_sha256', 'districts'), (),
                'Utah PP 610 inventory')
    if (document['schema_version'] != 1 or document['state'] != 'UT' or
            document['source_id'] != 'pp610' or
            document['dataset'] != 'ws11-utah-pp610-district-inventory'):
        raise UtahEvidenceError('Utah PP 610 inventory identity is invalid')
    text(document['review_scope'], 'Utah PP 610 inventory.review_scope',
         minimum=40)
    figure_sha = sha(document['figure_page_image_sha256'],
                     'Utah PP 610 inventory.figure_page_image_sha256')
    rows = document['districts']
    if not isinstance(rows, list) or len(rows) != len(PP610_FIGURE_25):
        raise UtahEvidenceError(
            'Utah PP 610 inventory must contain all 13 Figure 25 districts')
    out = []
    ids = set()
    for index, (row, expected) in enumerate(zip(rows, PP610_FIGURE_25), 1):
        label = f'Utah PP 610 inventory.districts[{index - 1}]'
        expect_keys(row, ('district_id', 'name', 'county', 'pdf_page',
                          'page_cite', 'verbatim_quote', 'quote_verbatim'), (),
                    label)
        district_id = identifier(row['district_id'], f'{label}.district_id')
        if district_id in ids:
            raise UtahEvidenceError(f'duplicate district_id {district_id}')
        ids.add(district_id)
        actual = (district_id, row['name'], row['county'],
                  row['verbatim_quote'])
        if actual != expected:
            raise UtahEvidenceError(
                f'{label} is not Figure 25 district {index}: expected {expected}')
        if row['pdf_page'] != 247 or row['pdf_page'] > sources['pp610']['pages']:
            raise UtahEvidenceError(f'{label}.pdf_page must be 247')
        if row['page_cite'] != 'Figure 25, p. 241':
            raise UtahEvidenceError(
                f'{label}.page_cite must bind Figure 25 on printed page 241')
        if row['quote_verbatim'] is not True:
            raise UtahEvidenceError(f'{label}.quote_verbatim must be true')
        out.append(dict(row))
    return out, figure_sha


def fetch_source(source):
    """Fetch one checksum-pinned USGS PDF without cross-host redirects."""
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
                raise UtahEvidenceError(
                    f'{source["source_id"]} redirected outside official USGS')
            remaining = source['bytes'] + 1
            while remaining:
                chunk = response.read(min(1 << 20, remaining))
                if not chunk:
                    break
                output.write(chunk)
                remaining -= len(chunk)
        if (temp_path.stat().st_size != source['bytes'] or
                sha256_file(temp_path) != source['sha256']):
            raise UtahEvidenceError(
                f'{source["source_id"]} download does not match reviewed hash')
        with open(temp_path, 'rb') as downloaded:
            if downloaded.read(5) != b'%PDF-':
                raise UtahEvidenceError(
                    f'{source["source_id"]} response is not a PDF')
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink()
    verify_pdf(source)
    return 'downloaded'


def page_record(source, pdf_page, cache):
    key = (source['source_id'], pdf_page)
    if key not in cache:
        if source['text_mode'] == 'ocr':
            raw, image_sha = extract_ocr_page(source, pdf_page)
        else:
            raw = extract_text_page(source, pdf_page)
            image_sha = None
        cache[key] = {
            'raw': raw,
            'text': raw.decode('utf-8', 'replace'),
            'page_text_sha256': sha256_bytes(raw),
            'page_image_sha256': image_sha,
        }
    return cache[key]


def render_page_image(source, pdf_page):
    if not shutil.which('pdftoppm'):
        raise UtahEvidenceError('pdftoppm is required for Figure 25 review')
    with tempfile.TemporaryDirectory(prefix='ut-pp610-page-') as directory:
        base = Path(directory) / 'page'
        run_tool([
            'pdftoppm', '-f', str(pdf_page), '-l', str(pdf_page), '-r', '300',
            '-png', '-singlefile', str(source['resolved_path']), str(base)],
            f'pdftoppm {source["source_id"]} page {pdf_page}')
        image = base.with_suffix('.png')
        if not image.is_file():
            raise UtahEvidenceError('pdftoppm did not create Figure 25 image')
        return sha256_file(image)


def build(sources_path=DEFAULT_SOURCES, reviewed_path=DEFAULT_REVIEWED,
          districts_path=DEFAULT_DISTRICTS, output=DEFAULT_OUTPUT):
    sources_document, sources_raw = load_json(
        Path(sources_path), 'Utah source inventory')
    reviewed_document, reviewed_raw = load_json(
        Path(reviewed_path), 'reviewed Utah grade evidence')
    districts_document, districts_raw = load_json(
        Path(districts_path), 'Utah PP 610 inventory')
    sources = validate_source_inventory(sources_document)
    mines, grade_source_ids = validate_reviewed(reviewed_document, sources)
    districts, figure_sha = validate_district_inventory(
        districts_document, sources)
    for source_id in sorted(sources):
        verify_pdf(sources[source_id])

    output = Path(output).resolve()
    if is_inside(output, SITE):
        raise UtahEvidenceError('Utah raw evidence output must stay outside site/')
    if output == ROOT or not is_inside(output, ROOT):
        raise UtahEvidenceError(
            'Utah raw evidence output must stay inside the workspace')

    page_cache = {}
    source_page_rows = {source_id: {} for source_id in grade_source_ids}
    grade_mines = []
    for mine in mines:
        out_mine = {
            key: mine[key] for key in ('mine_id', 'name', 'district', 'county')}
        out_mine['evidence'] = []
        evidence = mine['evidence'][0]
        source = sources[evidence['source_id']]
        page = page_record(source, evidence['pdf_page'], page_cache)
        if page['page_image_sha256'] != evidence['page_image_sha256']:
            raise UtahEvidenceError(
                f'{evidence["evidence_id"]}: reviewed page-image SHA-256 changed')
        score = quote_match_score(evidence['verbatim_quote'], page['text'])
        if score < MIN_QUOTE_MATCH:
            raise UtahEvidenceError(
                f'{evidence["evidence_id"]}: OCR quote/page match {score:.3f} '
                f'is below {MIN_QUOTE_MATCH:.2f}')
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
                'page_text_sha256': page['page_text_sha256'],
                'page_image_sha256': page['page_image_sha256'],
                'text_mode': 'ocr',
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
                'page_render': 'pdftoppm -r 300 -png',
                'ocr_arguments': 'tesseract --psm 6',
                'ocr_role': (
                    'diagnostic_quote_cross_check_page_image_sha_is_review_boundary'),
                'quote_match_floor': MIN_QUOTE_MATCH,
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
        'state': 'UT',
        'sources': [source_identities[source_id]
                    for source_id in sorted(source_identities)],
        'mines': grade_mines,
    }
    try:
        grade_validation = national.validate_grade_document(
            grades_document, 'UT', {}, '0' * 64)
    except national.PublicationError as exc:
        raise UtahEvidenceError(
            f'national grade contract rejected Utah: {exc}') from exc
    grades_artifact = atomic_json(output / 'grades/ut.json', grades_document)

    pp_source = sources['pp610']
    pp_page = page_record(pp_source, 247, page_cache)
    actual_figure_sha = render_page_image(pp_source, 247)
    if actual_figure_sha != figure_sha:
        raise UtahEvidenceError('PP 610 Figure 25 page-image SHA-256 changed')
    pp_districts = []
    pp_checks = []
    for district in districts:
        score = quote_word_coverage(
            district['verbatim_quote'], pp_page['text'])
        if score < 0.85:
            raise UtahEvidenceError(
                f'{district["district_id"]}: Figure 25 quote/page word '
                f'coverage {score:.3f} is below 0.85')
        pp_districts.append({
            'district_id': district['district_id'],
            'name': district['name'],
            'page_cite': district['page_cite'],
            'verbatim_quote': district['verbatim_quote'],
            'quote_verbatim': True,
            'page_text_sha256': pp_page['page_text_sha256'],
        })
        pp_checks.append({
            'district_id': district['district_id'],
            'figure_number': int(district['verbatim_quote'].split(',', 1)[0]),
            'quote_word_coverage': score,
        })
    pp_index = {
        'schema_version': 1,
        'source_id': 'pp610',
        'document_sha256': pp_source['sha256'],
        'extraction': {
            'scope': 'complete 13-entry Utah Figure 25 numbered legend',
            'pdf_page': 247,
            'printed_page': 241,
            'page_text': 'pdftotext -enc UTF-8 -layout',
            'page_text_sha256': pp_page['page_text_sha256'],
            'page_render': 'pdftoppm -r 300 -png',
            'page_image_sha256': actual_figure_sha,
        },
        'checks': pp_checks,
    }
    pp_index_sha = sha256_bytes(canonical_bytes(pp_index))
    pp_index_path = output / 'page-indexes' / f'pp610.{pp_index_sha}.json'
    page_index_artifacts['pp610'] = atomic_json(pp_index_path, pp_index)
    pp_document = {
        'schema_version': 1,
        'state': 'UT',
        'complete': True,
        'source': source_identity(pp_source, pp_index_sha),
        'districts': pp_districts,
    }
    try:
        pp_validation = national.validate_pp610_document(pp_document, 'UT')
    except national.PublicationError as exc:
        raise UtahEvidenceError(
            f'national PP 610 contract rejected Utah: {exc}') from exc
    pp_artifact = atomic_json(output / 'pp610/ut.json', pp_document)

    relative = lambda path: str(Path(path).resolve().relative_to(output))
    for artifact in [grades_artifact, pp_artifact,
                     *page_index_artifacts.values()]:
        artifact['path'] = relative(artifact['path'])
    report = {
        'schema_version': 1,
        'dataset': 'ws11-utah-grade-evidence-build',
        'state': 'UT',
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
            'tesseract': tool_version('tesseract'),
        },
        'metrics': {
            **grade_validation['metrics'],
            'pp610_districts': pp_validation['district_count'],
            'scan_image_pages_review_bound': sum(
                1 for page in page_cache.values()
                if page['page_image_sha256']),
            'pp610_figure_image_pages_review_bound': 1,
        },
        'threshold_observation': {
            'at_least_25_graded_mines': (
                grade_validation['metrics']['graded_mines'] >= 25),
            'at_least_2_primary_sources': (
                grade_validation['metrics']['primary_sources'] >= 2),
            'complete_pp610_anchor': pp_validation['district_count'] == 13,
            'is_release_decision': False,
        },
        'review_notes': {
            'district_averages_excluded': True,
            'grouped_claim_rows_excluded': True,
            'historic_dollar_values_converted': False,
            'triage_page_attributions_rechecked_against_numbered_pages': True,
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
    sources_document, _ = load_json(Path(sources_path), 'Utah source inventory')
    reviewed_document, _ = load_json(
        Path(reviewed_path), 'reviewed Utah grade evidence')
    districts_document, _ = load_json(
        Path(districts_path), 'Utah PP 610 inventory')
    sources = validate_source_inventory(sources_document)
    mines, used = validate_reviewed(reviewed_document, sources)
    districts, _ = validate_district_inventory(districts_document, sources)
    return {
        'mines': len(mines),
        'grade_sources': len(used),
        'pp610_districts': len(districts),
    }


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument('--sources', default=str(DEFAULT_SOURCES))
    result.add_argument('--reviewed', default=str(DEFAULT_REVIEWED))
    result.add_argument('--districts', default=str(DEFAULT_DISTRICTS))
    subparsers = result.add_subparsers(dest='command', required=True)
    subparsers.add_parser(
        'check', help='validate reviewed manifests without source PDFs')
    fetch = subparsers.add_parser(
        'fetch', help='fetch/verify official checksum-pinned PDFs')
    fetch.add_argument(
        '--all', action='store_true',
        help='accepted for parity; all four Utah sources are required')
    build_parser = subparsers.add_parser(
        'build', help='verify pages and produce private evidence inputs')
    build_parser.add_argument('--output', default=str(DEFAULT_OUTPUT))
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command == 'check':
            result = check_inputs(args.sources, args.reviewed, args.districts)
        elif args.command == 'fetch':
            source_document, _ = load_json(
                Path(args.sources), 'Utah source inventory')
            sources = validate_source_inventory(source_document)
            result = {
                source_id: fetch_source(sources[source_id])
                for source_id in sorted(sources)}
        else:
            report, artifact = build(
                args.sources, args.reviewed, args.districts, args.output)
            result = {'metrics': report['metrics'], 'build': artifact}
    except (UtahEvidenceError, OSError) as exc:
        print(f'Utah grade evidence ERROR: {exc}', file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
