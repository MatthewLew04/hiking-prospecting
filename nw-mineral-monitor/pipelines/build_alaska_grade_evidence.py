#!/usr/bin/env python3
"""Build checksum-bound Alaska WS11 grade, ARDF, and PP 610 evidence.

This is an evidence-only producer.  It verifies official DGGS/USGS PDFs,
binds every reviewed quotation to a deterministic 300-dpi page render,
preserves a conservative ARDF target crosswalk, and emits private inputs for
the national WS9 compiler.  It never writes below ``site/`` and never changes
state registries, release flags, coverage, manifests, or DONE state.

Alaska's PP 610 anchor is the complete 43-district Figure 5 inventory.  ARDF
is the occurrence backbone, but it is not substituted for official report
page provenance for grades.
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

import build_colorado_grade_evidence as shared
import build_national_grade_evidence as national
import build_nevada_grade_evidence as extraction


HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SITE = ROOT / 'site'
DEFAULT_SOURCES = ROOT / 'pipelines/config/ak_grade_sources.json'
DEFAULT_REVIEWED = ROOT / 'grades-research/ak/reviewed_grade_evidence.json'
DEFAULT_DISTRICTS = ROOT / 'grades-research/ak/pp610_district_inventory.json'
DEFAULT_ARDF = ROOT / 'grades-research/ak/ardf_target_crosswalk.json'
DEFAULT_OUTPUT = ROOT / 'build-inputs/ws9/ak-grade-evidence'
OFFICIAL_HOSTS = frozenset(('dggs.alaska.gov', 'pubs.usgs.gov'))
SOURCE_IDS = frozenset(('atdm-mr191-5', 'pp610', 'usbm-ofr50-94'))
GRADE_SOURCE_IDS = frozenset(('atdm-mr191-5', 'usbm-ofr50-94'))
SOURCE_PINS = {
    'atdm-mr191-5': (
        3614781,
        'dc8f364af1f8c72f91eb36e1f77cd53ca55ab74beb90d8170ec526e1a5bd42ad',
        98, 'embedded_scan'),
    'pp610': (
        39132819,
        'f4c1f048aaffe1e8d1431983e0a7b3f1bb543fab0f5380cd42e85c0a6a840896',
        290, 'embedded'),
    'usbm-ofr50-94': (
        8028244,
        '844be116be0e97bdddea12dd35cce87ea18fe9f4699ae8e492f04a6825011ed9',
        194, 'embedded_scan'),
}
SOURCE_KEYS = frozenset((
    'source_id', 'title', 'authority', 'citation', 'publication_year',
    'catalog_url', 'document_url', 'local_path', 'bytes', 'sha256', 'pages',
    'text_mode'))
REVIEW_KEYS = frozenset((
    'schema_version', 'dataset', 'state', 'status', 'reviewed_on',
    'reviewed_by', 'review_method', 'mines'))
MINE_KEYS = frozenset(('mine_id', 'name', 'district', 'county', 'evidence'))
EVIDENCE_KEYS = frozenset((
    'evidence_id', 'source_id', 'pdf_page', 'page_cite', 'verbatim_quote',
    'quote_verbatim', 'page_image_sha256', 'measurements', 'basis', 'years'))
ARDF_SERVICE_URL = (
    'https://services.arcgis.com/v01gqwM5QqNysAAi/ArcGIS/rest/services/'
    'ARDF_features/FeatureServer/0')
ARDF_KEYS = frozenset((
    'schema_version', 'dataset', 'state', 'status', 'reviewed_on', 'authority',
    'service_url', 'service_metadata_raw_sha256', 'records_sha256',
    'review_scope', 'records', 'targets'))
ARDF_RECORD_KEYS = frozenset((
    'ardf_no', 'object_id', 'site', 'quad_250', 'quad_63360', 'district',
    'commodities_main', 'latitude', 'longitude', 'primary_reference',
    'last_report_date'))
SHA_RE = re.compile(r'[0-9a-f]{64}')
ID_RE = re.compile(r'[a-z0-9][a-z0-9_.:-]{0,127}')
MIN_DIAGNOSTIC_QUOTE_MATCH = 0.40
MIN_PP610_DIAGNOSTIC_WORD_COVERAGE = 0.50
PP610_FIGURE_5_IMAGE_SHA256 = (
    'd40c49349862becee868cf6afdf7ba87b002d95bba125e732c84652fe0dc4e85')
ARDF_SERVICE_METADATA_SHA256 = (
    '9d83142a2b1abf145fde09054e9aff8e04e1d90bf62d2f03da66e02c2ab5e290')
ARDF_RECORDS_SHA256 = (
    '9449d184938389de6d1368dd047203cd9909ffce134e2fb18e95e7810448c7e5')

MINE_IDS = (
    'ak-blackjack-prospect',
    'ak-brown-bear-prospect',
    'ak-cedar-bay-ridge-occurrence',
    'ak-columbia-red-metals-prospect',
    'ak-dado-no-1-prospect',
    'ak-finski-bay-prospect',
    'ak-four-in-one-prospect',
    'ak-glendenning-prospect',
    'ak-globe-prospect',
    'ak-idle-claim-prospect',
    'ak-long-bay-no-1-occurrence',
    'ak-miners-river-discovery-prospect',
    'ak-miners-river-nickel-prospect',
    'ak-miners-river-no-1-occurrence',
    'ak-miners-river-no-2-occurrence',
    'ak-saddle-occurrence',
    'ak-slipper-point-occurrence',
    'ak-wells-bay-prospect',
    'ak-salt-chuck-mine',
    'ak-copper-center-prospect',
    'ak-shepard-mine',
    'ak-rich-hill-mine',
    'ak-bohemia-basin',
    'ak-fleming-island-deposit',
    'ak-snipe-bay-deposit',
    'ak-mertie-lode',
)

# Figure 5 is the complete Alaska PP 610 anchor.  Exact ordered identities
# prevent a plausible-looking subset or a transcribed map label from passing.
PP610_FIGURE_5 = (
    ('ak-kenai-peninsula', 'Kenai Peninsula', 'Cook Inlet-Susitna'),
    ('ak-valdez-creek', 'Valdez Creek', 'Cook Inlet-Susitna'),
    ('ak-willow-creek', 'Willow Creek', 'Cook Inlet-Susitna'),
    ('ak-yentna-cache-creek', 'Yentna-Cache Creek', 'Cook Inlet-Susitna'),
    ('ak-chistochina', 'Chistochina', 'Copper River'),
    ('ak-nizina', 'Nizina', 'Copper River'),
    ('ak-georgetown', 'Georgetown', 'Kuskokwim'),
    ('ak-goodnews-bay', 'Goodnews Bay', 'Kuskokwim'),
    ('ak-mckinley', 'McKinley', 'Kuskokwim'),
    ('ak-tuluksak-aniak', 'Tuluksak-Aniak', 'Kuskokwim'),
    ('ak-shungnak', 'Shungnak', 'Northwestern Alaska'),
    ('ak-council', 'Council', 'Seward Peninsula'),
    ('ak-fairhaven', 'Fairhaven', 'Seward Peninsula'),
    ('ak-kougarok', 'Kougarok', 'Seward Peninsula'),
    ('ak-koyuk', 'Koyuk', 'Seward Peninsula'),
    ('ak-nome', 'Nome', 'Seward Peninsula'),
    ('ak-port-clarence', 'Port Clarence', 'Seward Peninsula'),
    ('ak-solomon-bluff', 'Solomon-Bluff', 'Seward Peninsula'),
    ('ak-chichagof', 'Chichagof', 'Southeastern Alaska'),
    ('ak-juneau', 'Juneau', 'Southeastern Alaska'),
    ('ak-ketchikan-hyder', 'Ketchikan-Hyder', 'Southeastern Alaska'),
    ('ak-porcupine', 'Porcupine', 'Southeastern Alaska'),
    ('ak-yakataga', 'Yakataga', 'Southeastern Alaska'),
    ('ak-unga', 'Unga', 'Southwestern Alaska'),
    ('ak-bonnifield', 'Bonnifield', 'Yukon'),
    ('ak-chandalar', 'Chandalar', 'Yukon'),
    ('ak-chisana', 'Chisana', 'Yukon'),
    ('ak-circle', 'Circle', 'Yukon'),
    ('ak-eagle', 'Eagle', 'Yukon'),
    ('ak-fairbanks', 'Fairbanks', 'Yukon'),
    ('ak-fortymile', 'Fortymile', 'Yukon'),
    ('ak-iditarod', 'Iditarod', 'Yukon'),
    ('ak-innoko', 'Innoko', 'Yukon'),
    ('ak-hot-springs', 'Hot Springs', 'Yukon'),
    ('ak-kantishna', 'Kantishna', 'Yukon'),
    ('ak-koyukuk', 'Koyukuk', 'Yukon'),
    ('ak-marshall', 'Marshall', 'Yukon'),
    ('ak-nabesna', 'Nabesna', 'Yukon'),
    ('ak-rampart', 'Rampart', 'Yukon'),
    ('ak-ruby', 'Ruby', 'Yukon'),
    ('ak-richardson', 'Richardson', 'Yukon'),
    ('ak-tolovana', 'Tolovana', 'Yukon'),
    ('ak-port-valdez', 'Port Valdez', 'Prince William Sound'),
)

ARDF_RECORD_IDS = (
    'AN144', 'AN145', 'AN146', 'AN147', 'AN148', 'AN149', 'AN150', 'AN155',
    'AN156', 'AN158', 'AN159', 'CR049', 'CR051', 'CR063', 'JU225', 'PA034',
    'SI001', 'SI040', 'SR111', 'SR112', 'SR118')
ARDF_TARGETS = {
    'ak-blackjack-prospect': 'SR111',
    'ak-bohemia-basin': 'SI001',
    'ak-brown-bear-prospect': 'AN146',
    'ak-cedar-bay-ridge-occurrence': 'SR111',
    'ak-columbia-red-metals-prospect': 'AN159',
    'ak-copper-center-prospect': 'CR051',
    'ak-dado-no-1-prospect': 'AN148',
    'ak-finski-bay-prospect': 'SR118',
    'ak-fleming-island-deposit': 'SI040',
    'ak-four-in-one-prospect': 'AN148',
    'ak-glendenning-prospect': 'SR112',
    'ak-globe-prospect': 'AN155',
    'ak-idle-claim-prospect': 'AN158',
    'ak-long-bay-no-1-occurrence': 'AN156',
    'ak-mertie-lode': 'JU225',
    'ak-miners-river-discovery-prospect': 'AN145',
    'ak-miners-river-nickel-prospect': 'AN144',
    'ak-miners-river-no-1-occurrence': 'AN147',
    'ak-miners-river-no-2-occurrence': 'AN149',
    'ak-rich-hill-mine': 'CR063',
    'ak-saddle-occurrence': None,
    'ak-salt-chuck-mine': 'CR049',
    'ak-shepard-mine': None,
    'ak-slipper-point-occurrence': None,
    'ak-snipe-bay-deposit': 'PA034',
    'ak-wells-bay-prospect': 'AN150',
}


class AlaskaEvidenceError(ValueError):
    """An Alaska source or reviewed row violates the evidence contract."""


def canonical_bytes(value):
    try:
        return json.dumps(value, sort_keys=True, separators=(',', ':'),
                          ensure_ascii=False, allow_nan=False).encode('utf-8')
    except (TypeError, ValueError) as exc:
        raise AlaskaEvidenceError(f'value is not canonical JSON: {exc}') from exc


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
        raise AlaskaEvidenceError(str(exc)) from exc


def expect_keys(value, required, optional, label):
    if not isinstance(value, dict):
        raise AlaskaEvidenceError(f'{label} must be an object')
    missing = sorted(set(required) - set(value))
    extra = sorted(set(value) - set(required) - set(optional))
    if missing or extra:
        raise AlaskaEvidenceError(
            f'{label} keys mismatch: missing={missing}, extra={extra}')


def text(value, label, minimum=1, maximum=5000):
    if (not isinstance(value, str) or value != value.strip() or
            not minimum <= len(value) <= maximum or '\x00' in value):
        raise AlaskaEvidenceError(
            f'{label} must be trimmed text of length {minimum}..{maximum}')
    return value


def identifier(value, label):
    text(value, label, maximum=128)
    if ID_RE.fullmatch(value) is None:
        raise AlaskaEvidenceError(f'{label} must be a lowercase stable identifier')
    return value


def sha(value, label):
    if not isinstance(value, str) or SHA_RE.fullmatch(value) is None:
        raise AlaskaEvidenceError(f'{label} must be a lowercase SHA-256')
    return value


def positive_number(value, label):
    if (not isinstance(value, (int, float)) or isinstance(value, bool) or
            not math.isfinite(value) or value <= 0):
        raise AlaskaEvidenceError(f'{label} must be a positive finite number')


def is_inside(path, parent):
    try:
        return os.path.commonpath(
            (str(path.resolve()), str(parent.resolve()))) == str(parent.resolve())
    except (OSError, ValueError):
        return False


def official_url(value, label):
    text(value, label, maximum=2048)
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme != 'https' or parsed.hostname not in OFFICIAL_HOSTS:
        raise AlaskaEvidenceError(
            f'{label} must use HTTPS on an approved official DGGS/USGS host')
    if parsed.username or parsed.password or parsed.fragment:
        raise AlaskaEvidenceError(f'{label} contains forbidden URL components')


def validate_source_inventory(document):
    expect_keys(document, ('schema_version', 'dataset', 'sources'), (),
                'Alaska source inventory')
    if (document['schema_version'] != 1 or
            document['dataset'] != 'ws11-alaska-grade-source-inventory'):
        raise AlaskaEvidenceError('Alaska source inventory identity is invalid')
    rows = document['sources']
    if not isinstance(rows, list):
        raise AlaskaEvidenceError('Alaska source inventory sources must be a list')
    sources = {}
    for index, row in enumerate(rows):
        label = f'Alaska source inventory.sources[{index}]'
        expect_keys(row, SOURCE_KEYS, (), label)
        source_id = identifier(row['source_id'], f'{label}.source_id')
        if source_id in sources:
            raise AlaskaEvidenceError(f'duplicate source_id {source_id}')
        for field in ('title', 'authority', 'citation'):
            text(row[field], f'{label}.{field}', minimum=3, maximum=1000)
        year = row['publication_year']
        if (not isinstance(year, int) or isinstance(year, bool) or
                not 1800 <= year <= 2100):
            raise AlaskaEvidenceError(f'{label}.publication_year is invalid')
        official_url(row['catalog_url'], f'{label}.catalog_url')
        official_url(row['document_url'], f'{label}.document_url')
        local = Path(text(row['local_path'], f'{label}.local_path', maximum=500))
        if local.is_absolute() or '..' in local.parts or '.' in local.parts:
            raise AlaskaEvidenceError(f'{label}.local_path must be normalized and relative')
        resolved = (ROOT / local).resolve()
        if not is_inside(resolved, ROOT / 'pipelines/cache/ak-grade-sources'):
            raise AlaskaEvidenceError(
                f'{label}.local_path must stay in pipelines/cache/ak-grade-sources')
        if (not isinstance(row['bytes'], int) or isinstance(row['bytes'], bool) or
                row['bytes'] <= 0):
            raise AlaskaEvidenceError(f'{label}.bytes must be a positive integer')
        sha(row['sha256'], f'{label}.sha256')
        if (not isinstance(row['pages'], int) or isinstance(row['pages'], bool) or
                row['pages'] <= 0):
            raise AlaskaEvidenceError(f'{label}.pages must be a positive integer')
        expected_mode = 'embedded' if source_id == 'pp610' else 'embedded_scan'
        if row['text_mode'] != expected_mode:
            raise AlaskaEvidenceError(
                f'{label}.text_mode must be {expected_mode} for {source_id}')
        sources[source_id] = dict(row, resolved_path=resolved)
    if set(sources) != SOURCE_IDS:
        raise AlaskaEvidenceError(
            'Alaska source inventory must contain exactly both grade reports and pp610')
    for source_id, expected in SOURCE_PINS.items():
        source = sources[source_id]
        actual = (source['bytes'], source['sha256'], source['pages'],
                  source['text_mode'])
        if actual != expected:
            raise AlaskaEvidenceError(
                f'{source_id} differs from the reviewed source pin')
    return sources


def validate_reviewed(document, sources):
    expect_keys(document, REVIEW_KEYS, (), 'reviewed Alaska grade evidence')
    if (document['schema_version'] != 1 or
            document['dataset'] != 'ws11-alaska-reviewed-grade-extraction' or
            document['state'] != 'AK' or document['status'] != 'reviewed'):
        raise AlaskaEvidenceError('reviewed Alaska grade evidence identity is invalid')
    for field in ('reviewed_on', 'reviewed_by', 'review_method'):
        text(document[field], f'reviewed Alaska grade evidence.{field}', minimum=3)
    mines = document['mines']
    if not isinstance(mines, list) or len(mines) != len(MINE_IDS):
        raise AlaskaEvidenceError('reviewed Alaska evidence must contain exactly 26 mines')
    if tuple(row.get('mine_id') for row in mines) != MINE_IDS:
        raise AlaskaEvidenceError('reviewed Alaska mine identities/order differ from review')
    mine_names = set()
    evidence_ids = set()
    used_sources = set()
    checked = []
    for mine_index, mine in enumerate(mines):
        label = f'reviewed Alaska grade evidence.mines[{mine_index}]'
        expect_keys(mine, MINE_KEYS, (), label)
        mine_id = identifier(mine['mine_id'], f'{label}.mine_id')
        for field in ('name', 'district', 'county'):
            text(mine[field], f'{label}.{field}', minimum=2, maximum=300)
        name_key = (re.sub(r'[^a-z0-9]+', ' ', mine['name'].lower()).strip(),
                    re.sub(r'[^a-z0-9]+', ' ', mine['district'].lower()).strip())
        if name_key in mine_names:
            raise AlaskaEvidenceError(f'duplicate normalized mine/district {mine["name"]!r}')
        mine_names.add(name_key)
        rows = mine['evidence']
        if not isinstance(rows, list) or len(rows) != 1:
            raise AlaskaEvidenceError(
                f'{label}.evidence must contain exactly one reviewed source-page row')
        evidence = rows[0]
        ev_label = f'{label}.evidence[0]'
        expect_keys(evidence, EVIDENCE_KEYS, (), ev_label)
        evidence_id = identifier(evidence['evidence_id'], f'{ev_label}.evidence_id')
        if evidence_id in evidence_ids:
            raise AlaskaEvidenceError(f'duplicate evidence_id {evidence_id}')
        evidence_ids.add(evidence_id)
        source_id = identifier(evidence['source_id'], f'{ev_label}.source_id')
        if source_id not in GRADE_SOURCE_IDS or source_id not in sources:
            raise AlaskaEvidenceError(f'{ev_label} references a non-grade source')
        used_sources.add(source_id)
        page = evidence['pdf_page']
        if (not isinstance(page, int) or isinstance(page, bool) or
                not 1 <= page <= sources[source_id]['pages']):
            raise AlaskaEvidenceError(f'{ev_label}.pdf_page is outside the source')
        page_cite = text(evidence['page_cite'], f'{ev_label}.page_cite',
                         minimum=2, maximum=200)
        if not any(character.isdigit() for character in page_cite):
            raise AlaskaEvidenceError(f'{ev_label}.page_cite needs a numbered page')
        text(evidence['verbatim_quote'], f'{ev_label}.verbatim_quote', minimum=8)
        if evidence['quote_verbatim'] is not True:
            raise AlaskaEvidenceError(f'{ev_label}.quote_verbatim must be true')
        sha(evidence['page_image_sha256'], f'{ev_label}.page_image_sha256')
        measurements = evidence['measurements']
        if not isinstance(measurements, list) or not measurements:
            raise AlaskaEvidenceError(f'{ev_label}.measurements must be nonempty')
        commodities = set()
        for measurement_index, measurement in enumerate(measurements):
            measurement_label = f'{ev_label}.measurements[{measurement_index}]'
            expect_keys(measurement, ('commodity', 'value', 'unit'), (),
                        measurement_label)
            commodity = measurement['commodity']
            if commodity not in national.COMMODITIES or commodity in commodities:
                raise AlaskaEvidenceError(
                    f'{measurement_label}.commodity is unsupported or duplicated')
            commodities.add(commodity)
            positive_number(measurement['value'], f'{measurement_label}.value')
            if measurement['unit'] not in national.NATIVE_UNITS[commodity]:
                raise AlaskaEvidenceError(f'{measurement_label}.unit is invalid')
        for field in ('basis', 'years'):
            text(evidence[field], f'{ev_label}.{field}', maximum=500)
        checked.append({
            **{key: mine[key] for key in ('mine_id', 'name', 'district', 'county')},
            'evidence': [dict(evidence)],
        })
    if used_sources != GRADE_SOURCE_IDS:
        raise AlaskaEvidenceError(
            'Alaska reviewed grades must use both official grade reports')
    return checked, used_sources


def validate_district_inventory(document, sources):
    expect_keys(document, (
        'schema_version', 'dataset', 'state', 'source_id', 'review_scope',
        'figure_pdf_page', 'figure_printed_page', 'figure_page_image_sha256',
        'districts'), (), 'Alaska PP 610 inventory')
    if (document['schema_version'] != 1 or document['state'] != 'AK' or
            document['source_id'] != 'pp610' or
            document['dataset'] != 'ws11-alaska-pp610-district-inventory'):
        raise AlaskaEvidenceError('Alaska PP 610 inventory identity is invalid')
    text(document['review_scope'], 'Alaska PP 610 inventory.review_scope', minimum=40)
    if document['figure_pdf_page'] != 16 or document['figure_printed_page'] != 10:
        raise AlaskaEvidenceError('Alaska PP 610 inventory must bind Figure 5 on PDF page 16')
    figure_sha = sha(document['figure_page_image_sha256'],
                     'Alaska PP 610 inventory.figure_page_image_sha256')
    if figure_sha != PP610_FIGURE_5_IMAGE_SHA256:
        raise AlaskaEvidenceError('Alaska PP 610 Figure 5 review hash changed')
    rows = document['districts']
    if not isinstance(rows, list) or len(rows) != len(PP610_FIGURE_5):
        raise AlaskaEvidenceError(
            'Alaska PP 610 inventory must contain all 43 Figure 5 districts')
    quotes = set()
    out = []
    for index, (row, expected) in enumerate(zip(rows, PP610_FIGURE_5), 1):
        label = f'Alaska PP 610 inventory.districts[{index - 1}]'
        expect_keys(row, ('district_id', 'name', 'region', 'figure_number',
                          'verbatim_quote', 'quote_verbatim'), (), label)
        actual = (row['district_id'], row['name'], row['region'])
        if actual != expected or row['figure_number'] != index:
            raise AlaskaEvidenceError(
                f'{label} is not Figure 5 district {index}: expected {expected}')
        identifier(row['district_id'], f'{label}.district_id')
        for field in ('name', 'region'):
            text(row[field], f'{label}.{field}', minimum=2, maximum=300)
        quote = text(row['verbatim_quote'], f'{label}.verbatim_quote',
                     minimum=8, maximum=300)
        if row['quote_verbatim'] is not True:
            raise AlaskaEvidenceError(f'{label}.quote_verbatim must be true')
        if quote in quotes:
            raise AlaskaEvidenceError(f'{label} duplicates a Figure 5 quote')
        if not quote.startswith(f'{index}, ') or row['name'] not in quote:
            raise AlaskaEvidenceError(f'{label} quote does not bind its number/name')
        quotes.add(quote)
        out.append(dict(row))
    return out, figure_sha


def validate_ardf_crosswalk(document, mine_ids):
    expect_keys(document, ARDF_KEYS, (), 'Alaska ARDF crosswalk')
    if (document['schema_version'] != 1 or
            document['dataset'] != 'ws11-alaska-ardf-target-crosswalk' or
            document['state'] != 'AK' or document['status'] != 'reviewed'):
        raise AlaskaEvidenceError('Alaska ARDF crosswalk identity is invalid')
    text(document['reviewed_on'], 'Alaska ARDF crosswalk.reviewed_on', minimum=3)
    text(document['authority'], 'Alaska ARDF crosswalk.authority', minimum=10)
    text(document['review_scope'], 'Alaska ARDF crosswalk.review_scope', minimum=40)
    if document['service_url'] != ARDF_SERVICE_URL:
        raise AlaskaEvidenceError('Alaska ARDF crosswalk must use the official service')
    metadata_sha = sha(document['service_metadata_raw_sha256'],
                       'Alaska ARDF crosswalk.service_metadata_raw_sha256')
    if metadata_sha != ARDF_SERVICE_METADATA_SHA256:
        raise AlaskaEvidenceError('Alaska ARDF service metadata review hash changed')
    records_sha = sha(document['records_sha256'],
                      'Alaska ARDF crosswalk.records_sha256')
    if records_sha != ARDF_RECORDS_SHA256:
        raise AlaskaEvidenceError('Alaska ARDF reviewed record-set hash changed')
    records = document['records']
    if not isinstance(records, list) or len(records) != len(ARDF_RECORD_IDS):
        raise AlaskaEvidenceError('Alaska ARDF crosswalk must retain exactly 21 records')
    if sha256_bytes(canonical_bytes(records)) != records_sha:
        raise AlaskaEvidenceError('Alaska ARDF records canonical SHA-256 changed')
    if tuple(row.get('ardf_no') for row in records) != ARDF_RECORD_IDS:
        raise AlaskaEvidenceError('Alaska ARDF record identities/order changed')
    object_ids = set()
    for index, row in enumerate(records):
        label = f'Alaska ARDF crosswalk.records[{index}]'
        expect_keys(row, ARDF_RECORD_KEYS, (), label)
        text(row['ardf_no'], f'{label}.ardf_no', minimum=5, maximum=5)
        oid = row['object_id']
        if not isinstance(oid, int) or isinstance(oid, bool) or oid <= 0 or oid in object_ids:
            raise AlaskaEvidenceError(f'{label}.object_id must be a unique positive integer')
        object_ids.add(oid)
        for field in ('site', 'quad_250', 'quad_63360', 'commodities_main',
                      'primary_reference', 'last_report_date'):
            text(row[field], f'{label}.{field}', minimum=1, maximum=1000)
        text(row['district'], f'{label}.district', minimum=0, maximum=300)
        for field, lower, upper in (
                ('latitude', 51, 72), ('longitude', -180, -129)):
            value = row[field]
            if (not isinstance(value, (int, float)) or isinstance(value, bool) or
                    not math.isfinite(value) or not lower <= value <= upper):
                raise AlaskaEvidenceError(f'{label}.{field} is outside Alaska bounds')
    targets = document['targets']
    if not isinstance(targets, list) or len(targets) != len(ARDF_TARGETS):
        raise AlaskaEvidenceError('Alaska ARDF crosswalk must decide all 26 targets')
    if tuple(row.get('mine_id') for row in targets) != tuple(ARDF_TARGETS):
        raise AlaskaEvidenceError('Alaska ARDF target identities/order changed')
    if set(mine_ids) != set(ARDF_TARGETS):
        raise AlaskaEvidenceError('Alaska grade targets and ARDF decisions differ')
    linked = 0
    unmatched = 0
    for index, row in enumerate(targets):
        mine_id = row['mine_id']
        expected = ARDF_TARGETS[mine_id]
        label = f'Alaska ARDF crosswalk.targets[{index}]'
        if expected is None:
            if row['status'] != 'no_unambiguous_record':
                raise AlaskaEvidenceError(f'{label} must preserve the explicit no-match')
            expect_keys(row, ('mine_id', 'status', 'finding'), (), label)
            text(row['finding'], f'{label}.finding', minimum=60, maximum=1000)
            unmatched += 1
        else:
            expect_keys(row, ('mine_id', 'status', 'ardf_no', 'match_basis'), (), label)
            if row['status'] != 'linked' or row['ardf_no'] != expected:
                raise AlaskaEvidenceError(f'{label} differs from the reviewed ARDF link')
            text(row['match_basis'], f'{label}.match_basis', minimum=10, maximum=1000)
            linked += 1
    if linked != 23 or unmatched != 3:
        raise AlaskaEvidenceError('Alaska ARDF crosswalk must retain 23 links and 3 findings')
    return {'document': document, 'records': len(records), 'linked': linked,
            'unmatched': unmatched, 'unique_linked_records': len({
                value for value in ARDF_TARGETS.values() if value is not None})}


def verify_pdf(source):
    try:
        shared.verify_pdf(source)
    except shared.ColoradoEvidenceError as exc:
        raise AlaskaEvidenceError(str(exc).replace('Colorado', 'Alaska')) from exc


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
                raise AlaskaEvidenceError(
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
            raise AlaskaEvidenceError(
                f'{source["source_id"]} download differs from reviewed bytes/SHA-256')
        with open(temp_path, 'rb') as downloaded:
            if downloaded.read(5) != b'%PDF-':
                raise AlaskaEvidenceError(f'{source["source_id"]} response is not a PDF')
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink()
    verify_pdf(source)
    return 'downloaded'


def page_record(source, page, cache):
    try:
        return shared.page_record(source, page, cache, image_bound=True)
    except shared.ColoradoEvidenceError as exc:
        raise AlaskaEvidenceError(str(exc).replace('Colorado', 'Alaska')) from exc


def source_identity(source, page_index_sha):
    return shared.source_identity(source, page_index_sha)


def atomic_json(path, document):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or path.parent.is_symlink():
        raise AlaskaEvidenceError(f'output path must not be a symlink: {path}')
    raw = canonical_bytes(document)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=path.name + '.',
                                     suffix='.part', delete=False) as output:
        output.write(raw)
        temp_name = output.name
    os.replace(temp_name, path)
    return {'path': str(path), 'bytes': len(raw), 'sha256': sha256_bytes(raw)}


def tool_version(command):
    return shared.tool_version(command)


def build(sources_path=DEFAULT_SOURCES, reviewed_path=DEFAULT_REVIEWED,
          districts_path=DEFAULT_DISTRICTS, ardf_path=DEFAULT_ARDF,
          output=DEFAULT_OUTPUT):
    sources_document, sources_raw = load_json(Path(sources_path), 'Alaska source inventory')
    reviewed_document, reviewed_raw = load_json(
        Path(reviewed_path), 'reviewed Alaska grade evidence')
    districts_document, districts_raw = load_json(
        Path(districts_path), 'Alaska PP 610 inventory')
    ardf_document, ardf_raw = load_json(Path(ardf_path), 'Alaska ARDF crosswalk')
    sources = validate_source_inventory(sources_document)
    mines, grade_source_ids = validate_reviewed(reviewed_document, sources)
    districts, figure_image_sha = validate_district_inventory(
        districts_document, sources)
    ardf = validate_ardf_crosswalk(ardf_document,
                                   [mine['mine_id'] for mine in mines])
    for source_id in sorted(sources):
        verify_pdf(sources[source_id])

    output = Path(output).resolve()
    if is_inside(output, SITE):
        raise AlaskaEvidenceError('Alaska raw evidence output must stay outside site/')
    if output == ROOT or not is_inside(output, ROOT):
        raise AlaskaEvidenceError('Alaska raw evidence output must stay inside workspace')
    page_cache = {}
    source_page_rows = {source_id: {} for source_id in sources}

    grade_mines = []
    for mine in mines:
        out_mine = {key: mine[key] for key in ('mine_id', 'name', 'district', 'county')}
        out_mine['evidence'] = []
        for evidence in mine['evidence']:
            source = sources[evidence['source_id']]
            page = page_record(source, evidence['pdf_page'], page_cache)
            if page['page_image_sha256'] != evidence['page_image_sha256']:
                raise AlaskaEvidenceError(
                    f'{evidence["evidence_id"]}: reviewed page-image SHA-256 changed')
            score = extraction.quote_match_score(
                evidence['verbatim_quote'], page['text'])
            if score < MIN_DIAGNOSTIC_QUOTE_MATCH:
                raise AlaskaEvidenceError(
                    f'{evidence["evidence_id"]}: OCR diagnostic quote/page match '
                    f'{score:.3f} is below {MIN_DIAGNOSTIC_QUOTE_MATCH:.2f}')
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
                    'text_mode': source['text_mode'],
                    'checks': [],
                })
            page_row['checks'].append({
                'evidence_id': evidence['evidence_id'],
                'page_cite': evidence['page_cite'],
                'quote_match_score': score,
                'review_boundary': 'page_image_sha256_human_review_ocr_diagnostic',
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
                'page_render': 'pdftoppm -r 300 -png -singlefile',
                'review_boundary': 'page_image_sha256',
                'text_role': 'diagnostic_only',
                'diagnostic_quote_match_floor': MIN_DIAGNOSTIC_QUOTE_MATCH,
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
        'state': 'AK',
        'sources': [source_identities[source_id]
                    for source_id in sorted(source_identities)],
        'mines': grade_mines,
    }
    try:
        grade_validation = national.validate_grade_document(
            grades_document, 'AK', {}, '0' * 64)
    except national.PublicationError as exc:
        raise AlaskaEvidenceError(
            f'national grade contract rejected Alaska: {exc}') from exc
    grades_artifact = atomic_json(output / 'grades/ak.json', grades_document)

    pp_source = sources['pp610']
    figure_page = page_record(pp_source, 16, page_cache)
    if figure_page['page_image_sha256'] != figure_image_sha:
        raise AlaskaEvidenceError('PP 610 Figure 5 reviewed page-image SHA-256 changed')
    pp_districts = []
    pp_checks = []
    for district in districts:
        score = extraction.quote_word_coverage(
            district['verbatim_quote'], figure_page['text'])
        if score < MIN_PP610_DIAGNOSTIC_WORD_COVERAGE:
            raise AlaskaEvidenceError(
                f'{district["district_id"]}: PP 610 Figure 5 word coverage '
                f'{score:.3f} is below '
                f'{MIN_PP610_DIAGNOSTIC_WORD_COVERAGE:.2f}')
        pp_districts.append({
            'district_id': district['district_id'],
            'name': district['name'],
            'page_cite': 'Figure 5, p. 10 (PDF p. 16)',
            'verbatim_quote': district['verbatim_quote'],
            'quote_verbatim': True,
            'page_text_sha256': figure_page['page_text_sha256'],
        })
        pp_checks.append({
            'district_id': district['district_id'],
            'figure_number': district['figure_number'],
            'region': district['region'],
            'quote_word_coverage': score,
        })
    pp_index = {
        'schema_version': 1,
        'source_id': 'pp610',
        'document_sha256': pp_source['sha256'],
        'reviewed_scope': 'complete Alaska Figure 5 inventory, all 43 numbered districts',
        'extraction': {
            'text_mode': 'embedded',
            'pdftotext_arguments': '-enc UTF-8 -layout',
            'page_render': 'pdftoppm -r 300 -png -singlefile',
            'review_boundary': 'page_image_sha256_human_visual_transcription',
        },
        'pages': [{
            'pdf_page': 16,
            'printed_page': 10,
            'page_text_sha256': figure_page['page_text_sha256'],
            'page_image_sha256': figure_page['page_image_sha256'],
            'checks': pp_checks,
        }],
    }
    pp_index_sha = sha256_bytes(canonical_bytes(pp_index))
    pp_index_path = output / 'page-indexes' / f'pp610.{pp_index_sha}.json'
    page_index_artifacts['pp610'] = atomic_json(pp_index_path, pp_index)
    pp_document = {
        'schema_version': 1,
        'state': 'AK',
        'complete': True,
        'source': source_identity(pp_source, pp_index_sha),
        'districts': pp_districts,
    }
    try:
        pp_validation = national.validate_pp610_document(pp_document, 'AK')
    except national.PublicationError as exc:
        raise AlaskaEvidenceError(
            f'national PP 610 contract rejected Alaska: {exc}') from exc
    pp_artifact = atomic_json(output / 'pp610/ak.json', pp_document)

    # This artifact remains private and preserves the reviewed service subset,
    # including explicit no-match findings.  Grade provenance stays in reports.
    ardf_artifact = atomic_json(
        output / 'backbone/ak-ardf-crosswalk.json', ardf['document'])

    relative = lambda path: str(Path(path).resolve().relative_to(output))
    all_artifacts = [grades_artifact, pp_artifact, ardf_artifact,
                     *page_index_artifacts.values()]
    for artifact in all_artifacts:
        artifact['path'] = relative(artifact['path'])
    grade_image_pages = sum(len(source_page_rows[source_id])
                            for source_id in grade_source_ids)
    report = {
        'schema_version': 1,
        'dataset': 'ws11-alaska-grade-evidence-build',
        'state': 'AK',
        'effect': 'evidence_only_no_release_or_done_mutation',
        'inputs': {
            'producer_sha256': sha256_file(Path(__file__).resolve()),
            'national_contract_sha256': sha256_file(
                ROOT / 'pipelines/build_national_grade_evidence.py'),
            'source_inventory_sha256': sha256_bytes(sources_raw),
            'reviewed_grade_evidence_sha256': sha256_bytes(reviewed_raw),
            'pp610_district_inventory_sha256': sha256_bytes(districts_raw),
            'ardf_target_crosswalk_sha256': sha256_bytes(ardf_raw),
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
            'grade_page_images_review_bound': grade_image_pages,
            'pp610_figure_pages_review_bound': 1,
            'ardf_targets_linked': ardf['linked'],
            'ardf_unique_records': ardf['unique_linked_records'],
            'ardf_explicit_unmatched_findings': ardf['unmatched'],
        },
        'threshold_observation': {
            'at_least_25_graded_mines': grade_validation['metrics']['graded_mines'] >= 25,
            'at_least_2_primary_sources': grade_validation['metrics']['primary_sources'] >= 2,
            'complete_pp610_anchor': pp_validation['district_count'] == 43,
            'all_grade_pages_image_bound': grade_image_pages == 24,
            'ardf_backbone_reviewed_without_guessed_links': (
                ardf['linked'] == 23 and ardf['unmatched'] == 3),
            'is_release_decision': False,
        },
        'artifacts': {
            'grade_input': grades_artifact,
            'pp610_input': pp_artifact,
            'ardf_backbone': ardf_artifact,
            'page_indexes': page_index_artifacts,
        },
    }
    report_artifact = atomic_json(output / 'build.json', report)
    return report, report_artifact


def check_inputs(sources_path=DEFAULT_SOURCES, reviewed_path=DEFAULT_REVIEWED,
                 districts_path=DEFAULT_DISTRICTS, ardf_path=DEFAULT_ARDF):
    sources_document, _ = load_json(Path(sources_path), 'Alaska source inventory')
    reviewed_document, _ = load_json(
        Path(reviewed_path), 'reviewed Alaska grade evidence')
    districts_document, _ = load_json(
        Path(districts_path), 'Alaska PP 610 inventory')
    ardf_document, _ = load_json(Path(ardf_path), 'Alaska ARDF crosswalk')
    sources = validate_source_inventory(sources_document)
    mines, used = validate_reviewed(reviewed_document, sources)
    districts, _ = validate_district_inventory(districts_document, sources)
    ardf = validate_ardf_crosswalk(ardf_document,
                                   [mine['mine_id'] for mine in mines])
    return {
        'mines': len(mines),
        'grade_sources': len(used),
        'pp610_districts': len(districts),
        'ardf_targets_linked': ardf['linked'],
        'ardf_unique_records': ardf['unique_linked_records'],
        'ardf_explicit_unmatched_findings': ardf['unmatched'],
    }


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument('--sources', default=str(DEFAULT_SOURCES))
    result.add_argument('--reviewed', default=str(DEFAULT_REVIEWED))
    result.add_argument('--districts', default=str(DEFAULT_DISTRICTS))
    result.add_argument('--ardf', default=str(DEFAULT_ARDF))
    subparsers = result.add_subparsers(dest='command', required=True)
    subparsers.add_parser('check', help='validate review manifests without source PDFs')
    fetch = subparsers.add_parser('fetch', help='fetch/verify official pinned PDFs')
    fetch.add_argument('--all', action='store_true',
                       help='accepted for parity; all three fixed sources are required')
    build_parser = subparsers.add_parser(
        'build', help='verify source pages and produce private inputs')
    build_parser.add_argument('--output', default=str(DEFAULT_OUTPUT))
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command == 'check':
            result = check_inputs(args.sources, args.reviewed, args.districts, args.ardf)
        elif args.command == 'fetch':
            sources_document, _ = load_json(Path(args.sources), 'Alaska source inventory')
            reviewed_document, _ = load_json(
                Path(args.reviewed), 'reviewed Alaska grade evidence')
            districts_document, _ = load_json(
                Path(args.districts), 'Alaska PP 610 inventory')
            ardf_document, _ = load_json(Path(args.ardf), 'Alaska ARDF crosswalk')
            sources = validate_source_inventory(sources_document)
            mines, _ = validate_reviewed(reviewed_document, sources)
            validate_district_inventory(districts_document, sources)
            validate_ardf_crosswalk(ardf_document,
                                    [mine['mine_id'] for mine in mines])
            result = {source_id: fetch_source(sources[source_id])
                      for source_id in sorted(sources)}
        else:
            report, artifact = build(args.sources, args.reviewed, args.districts,
                                     args.ardf, args.output)
            result = {'metrics': report['metrics'], 'build': artifact}
    except (AlaskaEvidenceError, OSError) as exc:
        print(f'Alaska grade evidence ERROR: {exc}', file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
