#!/usr/bin/env python3
"""Compile WS9 grade evidence for the exact 49-state WS11 registry.

This is deliberately an evidence publisher, not a research generator.  It
accepts only a private, checksum-pinned inventory of reviewed state evidence;
normalizes declared grade units and historic value-per-ton statements through
a separately reviewed six-commodity price configuration; and emits immutable,
content-addressed JSON plus a small mutable pointer.  It never edits state
registries, release flags, browser manifests, or map data.

The compiler supports honest progress: a state with fewer than 25 cited mines
is marked ``incomplete`` unless it carries a separately checksummed finding
based on at least two primary sources.  ``--require-done`` turns those visible
gaps into a build failure without enabling or releasing anything.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
import tempfile
from dataclasses import dataclass

from state_registry import load_states, validate_registry


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
SITE = os.path.join(ROOT, 'site')
DATASET = 'ws9-national-grade-evidence'
COMMODITIES = ('Au', 'Ag', 'Cu', 'Pb', 'Zn', 'Fe')
PRICE_UNITS = {
    'Au': 'nominal_usd_per_troy_ounce',
    'Ag': 'nominal_usd_per_troy_ounce',
    'Cu': 'nominal_usd_per_pound_contained_metal',
    'Pb': 'nominal_usd_per_pound_contained_metal',
    'Zn': 'nominal_usd_per_pound_contained_metal',
    'Fe': 'nominal_usd_per_pound_contained_metal',
}
CANONICAL_GRADE_UNITS = {
    'Au': 'troy_ounces_per_short_ton',
    'Ag': 'troy_ounces_per_short_ton',
    'Cu': 'percent',
    'Pb': 'percent',
    'Zn': 'percent',
    'Fe': 'percent',
}
NATIVE_UNITS = {
    'Au': frozenset(('troy_ounces_per_short_ton', 'grams_per_metric_tonne')),
    'Ag': frozenset(('troy_ounces_per_short_ton', 'grams_per_metric_tonne')),
    'Cu': frozenset(('percent', 'parts_per_million', 'pounds_per_short_ton')),
    'Pb': frozenset(('percent', 'parts_per_million', 'pounds_per_short_ton')),
    'Zn': frozenset(('percent', 'parts_per_million', 'pounds_per_short_ton')),
    'Fe': frozenset(('percent', 'parts_per_million', 'pounds_per_short_ton')),
}
GRAMS_PER_METRIC_TONNE_TO_TROY_OZ_PER_SHORT_TON = 0.0291666666667
ID_RE = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}')
SHA_RE = re.compile(r'[0-9a-f]{64}')
DATE_RE = re.compile(r'\d{4}-\d{2}-\d{2}')


class PublicationError(ValueError):
    """A source or publication violates the national grade contract."""


def _reject_constant(value):
    raise ValueError(f'non-standard JSON number {value}')


def _reject_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f'duplicate JSON object key {key!r}')
        result[key] = value
    return result


def strict_json_bytes(raw, label):
    try:
        return json.loads(raw, parse_constant=_reject_constant,
                          object_pairs_hook=_reject_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise PublicationError(f'{label} is not strict JSON: {exc}') from exc


def load_strict_json(path, label):
    try:
        with open(path, 'rb') as source:
            raw = source.read()
    except OSError as exc:
        raise PublicationError(f'cannot read {label}: {exc}') from exc
    return strict_json_bytes(raw, label), raw


def canonical_bytes(value):
    try:
        return json.dumps(value, sort_keys=True, separators=(',', ':'),
                          ensure_ascii=False, allow_nan=False).encode('utf-8')
    except (TypeError, ValueError) as exc:
        raise PublicationError(f'evidence is not canonical JSON: {exc}') from exc


def sha256_bytes(raw):
    return hashlib.sha256(raw).hexdigest()


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as source:
        for chunk in iter(lambda: source.read(1 << 20), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _expect_object(value, label):
    if not isinstance(value, dict):
        raise PublicationError(f'{label} must be an object')
    return value


def _expect_list(value, label):
    if not isinstance(value, list):
        raise PublicationError(f'{label} must be a list')
    return value


def _expect_keys(value, required, optional, label):
    _expect_object(value, label)
    keys = set(value)
    missing = sorted(set(required) - keys)
    extra = sorted(keys - set(required) - set(optional))
    if missing or extra:
        raise PublicationError(
            f'{label} keys mismatch: missing={missing}, extra={extra}')


def _text(value, label, minimum=1, maximum=5000):
    if (not isinstance(value, str) or value != value.strip() or
            not minimum <= len(value) <= maximum or '\x00' in value):
        raise PublicationError(
            f'{label} must be trimmed text of length {minimum}..{maximum}')
    return value


def _identifier(value, label):
    _text(value, label, maximum=128)
    if ID_RE.fullmatch(value) is None:
        raise PublicationError(f'{label} is not a stable identifier')
    return value


def _sha(value, label):
    if not isinstance(value, str) or SHA_RE.fullmatch(value) is None:
        raise PublicationError(f'{label} must be a lowercase SHA-256')
    return value


def _date(value, label):
    if not isinstance(value, str) or DATE_RE.fullmatch(value) is None:
        raise PublicationError(f'{label} must be YYYY-MM-DD')
    return value


def _https(value, label):
    _text(value, label, maximum=2048)
    if re.fullmatch(r'https://[^\s]+', value) is None:
        raise PublicationError(f'{label} must be an HTTPS URL')
    return value


def _number(value, label, minimum=0, positive=False):
    if (not isinstance(value, (int, float)) or isinstance(value, bool) or
            not math.isfinite(value) or value < minimum or
            (positive and value <= 0)):
        qualifier = 'positive finite' if positive else f'finite and >= {minimum}'
        raise PublicationError(f'{label} must be {qualifier}')
    return float(value)


def _canonical_name(value):
    return re.sub(r'[^a-z0-9]+', ' ', value.lower()).strip()


def _inside(path, parent):
    try:
        return os.path.commonpath((os.path.realpath(path),
                                   os.path.realpath(parent))) == os.path.realpath(parent)
    except ValueError:
        return False


def _assert_private(path, label):
    if _inside(path, SITE):
        raise PublicationError(
            f'{label} is inside public site/: raw grade staging must remain private')
    if os.path.islink(path):
        raise PublicationError(f'{label} must not be a symlink')


@dataclass(frozen=True)
class Artifact:
    label: str
    relative_path: str
    path: str
    bytes: int
    sha256: str


def _artifact_descriptor(value, staging_root, label):
    _expect_keys(value, ('path', 'bytes', 'sha256'), (), label)
    relative = _text(value['path'], f'{label}.path', maximum=500)
    if (os.path.isabs(relative) or relative.startswith(('/', '\\')) or
            any(part in ('', '.', '..') for part in relative.replace('\\', '/').split('/'))):
        raise PublicationError(f'{label}.path must be a normalized relative path')
    size = value['bytes']
    if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
        raise PublicationError(f'{label}.bytes must be a positive integer')
    expected_sha = _sha(value['sha256'], f'{label}.sha256')
    path = os.path.realpath(os.path.join(staging_root, relative))
    if not _inside(path, staging_root):
        raise PublicationError(f'{label}.path escapes the private inventory directory')
    _assert_private(path, label)
    try:
        actual_size = os.path.getsize(path)
        actual_sha = sha256_file(path)
    except OSError as exc:
        raise PublicationError(f'{label} artifact is unreadable: {exc}') from exc
    if actual_size != size or actual_sha != expected_sha:
        raise PublicationError(
            f'{label} checksum/size mismatch: expected {size}/{expected_sha}, '
            f'got {actual_size}/{actual_sha}')
    return Artifact(label, relative, path, size, expected_sha)


def _source_identity(value, label, expected_id=None):
    required = ('source_id', 'title', 'authority', 'url', 'primary',
                'document_sha256', 'page_index_sha256')
    optional = ('citation', 'publication_year')
    _expect_keys(value, required, optional, label)
    source_id = _identifier(value['source_id'], f'{label}.source_id')
    if expected_id is not None and source_id != expected_id:
        raise PublicationError(f'{label}.source_id must be {expected_id!r}')
    if value['primary'] is not True:
        raise PublicationError(f'{label} must identify a primary source')
    row = {
        'source_id': source_id,
        'title': _text(value['title'], f'{label}.title', minimum=3, maximum=500),
        'authority': _text(value['authority'], f'{label}.authority', maximum=300),
        'url': _https(value['url'], f'{label}.url'),
        'primary': True,
        'document_sha256': _sha(value['document_sha256'],
                                f'{label}.document_sha256'),
        'page_index_sha256': _sha(value['page_index_sha256'],
                                  f'{label}.page_index_sha256'),
    }
    if 'citation' in value:
        row['citation'] = _text(value['citation'], f'{label}.citation',
                                minimum=8, maximum=1000)
    if 'publication_year' in value:
        year = value['publication_year']
        if (not isinstance(year, int) or isinstance(year, bool) or
                not 1800 <= year <= 2100):
            raise PublicationError(f'{label}.publication_year is invalid')
        row['publication_year'] = year
    return row


def validate_price_config(document):
    label = 'price config'
    _expect_keys(document,
                 ('schema_version', 'status', 'reviewed_on', 'reviewed_by',
                  'commodities'), (), label)
    if document['schema_version'] != 1 or document['status'] != 'reviewed':
        raise PublicationError('price config must be schema 1 and status=reviewed')
    reviewed_on = _date(document['reviewed_on'], 'price config.reviewed_on')
    reviewed_by = _text(document['reviewed_by'], 'price config.reviewed_by',
                        minimum=3, maximum=300)
    commodities = _expect_object(document['commodities'],
                                 'price config.commodities')
    if set(commodities) != set(COMMODITIES):
        raise PublicationError(
            f'price config must cover exactly {list(COMMODITIES)}')
    normalized = {}
    for commodity in COMMODITIES:
        row = commodities[commodity]
        row_label = f'price config.commodities.{commodity}'
        _expect_keys(row, ('unit', 'annual', 'source'), (), row_label)
        if row['unit'] != PRICE_UNITS[commodity]:
            raise PublicationError(
                f'{row_label}.unit must be {PRICE_UNITS[commodity]!r}')
        source = _source_identity(row['source'], f'{row_label}.source')
        annual = _expect_object(row['annual'], f'{row_label}.annual')
        if not annual:
            raise PublicationError(f'{row_label}.annual must not be empty')
        prices = {}
        for year_text, price in annual.items():
            if not isinstance(year_text, str) or re.fullmatch(r'\d{4}', year_text) is None:
                raise PublicationError(f'{row_label}.annual year {year_text!r} is invalid')
            year = int(year_text)
            if not 1800 <= year <= 2100:
                raise PublicationError(f'{row_label}.annual year {year} is out of range')
            prices[year] = _number(price, f'{row_label}.annual.{year}', positive=True)
        years = sorted(prices)
        if years != list(range(years[0], years[-1] + 1)):
            raise PublicationError(f'{row_label}.annual must be a contiguous annual series')
        normalized[commodity] = {
            'unit': row['unit'],
            'annual': {str(year): prices[year] for year in years},
            'source': source,
        }
    return {
        'schema_version': 1,
        'status': 'reviewed',
        'reviewed_on': reviewed_on,
        'reviewed_by': reviewed_by,
        'commodities': normalized,
    }


def _page_evidence(value, label):
    page_cite = _text(value, label, minimum=2, maximum=200)
    if not any(character.isdigit() for character in page_cite):
        raise PublicationError(f'{label} must identify a numbered page/plate/sheet')
    return page_cite


def _normalize_native(commodity, value, unit):
    value = _number(value, f'{commodity} measurement value', positive=True)
    if unit not in NATIVE_UNITS[commodity]:
        raise PublicationError(
            f'{commodity} measurement unit {unit!r} is not one of '
            f'{sorted(NATIVE_UNITS[commodity])}')
    if unit == CANONICAL_GRADE_UNITS[commodity]:
        normalized = value
        method = 'identity'
    elif unit == 'grams_per_metric_tonne':
        normalized = value * GRAMS_PER_METRIC_TONNE_TO_TROY_OZ_PER_SHORT_TON
        method = 'grams_per_metric_tonne_x_0.0291666666667'
    elif unit == 'parts_per_million':
        normalized = value / 10_000
        method = 'parts_per_million_div_10000'
    elif unit == 'pounds_per_short_ton':
        normalized = value / 20
        method = 'pounds_per_short_ton_div_20'
    else:  # pragma: no cover - exhaustive unit contract above
        raise AssertionError(unit)
    return {
        'commodity': commodity,
        'value': round(normalized, 10),
        'unit': CANONICAL_GRADE_UNITS[commodity],
        'input_value': value,
        'input_unit': unit,
        'method': method,
    }


def _normalize_historic(value, prices, price_config_sha):
    label = 'historic value-per-ton'
    _expect_keys(value, ('commodity', 'value', 'unit', 'price_year'), (), label)
    commodity = value['commodity']
    if commodity not in COMMODITIES:
        raise PublicationError(f'{label}.commodity {commodity!r} is unsupported')
    if value['unit'] != 'nominal_usd_per_short_ton':
        raise PublicationError(
            f'{label}.unit must be nominal_usd_per_short_ton')
    dollars = _number(value['value'], f'{label}.value', positive=True)
    year = value['price_year']
    if (not isinstance(year, int) or isinstance(year, bool) or
            not 1800 <= year <= 2100):
        raise PublicationError(f'{label}.price_year is invalid')
    price_row = prices['commodities'][commodity]
    price = price_row['annual'].get(str(year))
    if price is None:
        raise PublicationError(
            f'{label} {commodity} year {year} is absent from the reviewed annual series')
    if commodity in ('Au', 'Ag'):
        normalized = dollars / price
        method = 'usd_per_short_ton_div_usd_per_troy_ounce'
    else:
        normalized = dollars / (20 * price)
        method = 'usd_per_short_ton_div_20lb_per_one_percent_div_usd_per_lb'
    return {
        'commodity': commodity,
        'value': round(normalized, 10),
        'unit': CANONICAL_GRADE_UNITS[commodity],
        'input_value': dollars,
        'input_unit': value['unit'],
        'method': method,
        'price': {
            'year': year,
            'value': price,
            'unit': price_row['unit'],
            'source_id': price_row['source']['source_id'],
            'price_config_sha256': price_config_sha,
        },
    }


def validate_grade_document(document, code, prices, price_config_sha):
    label = f'{code} grade evidence'
    _expect_keys(document, ('schema_version', 'state', 'sources', 'mines'), (), label)
    if document['schema_version'] != 1 or document['state'] != code:
        raise PublicationError(f'{label} schema/state identity is invalid')
    source_rows = _expect_list(document['sources'], f'{label}.sources')
    sources = {}
    for index, value in enumerate(source_rows):
        source = _source_identity(value, f'{label}.sources[{index}]')
        if source['source_id'] in sources:
            raise PublicationError(f'{label} has duplicate source_id {source["source_id"]}')
        sources[source['source_id']] = source
    mines = []
    mine_ids = set()
    mine_names = set()
    evidence_ids = set()
    evidence_signatures = set()
    used_sources = set()
    for mine_index, value in enumerate(_expect_list(document['mines'],
                                                     f'{label}.mines')):
        mine_label = f'{label}.mines[{mine_index}]'
        _expect_keys(value, ('mine_id', 'name', 'evidence'),
                     ('district', 'county'), mine_label)
        mine_id = _identifier(value['mine_id'], f'{mine_label}.mine_id')
        if mine_id in mine_ids:
            raise PublicationError(f'{label} has duplicate mine_id {mine_id}')
        mine_ids.add(mine_id)
        name = _text(value['name'], f'{mine_label}.name', minimum=2, maximum=300)
        district = (_text(value['district'], f'{mine_label}.district', maximum=300)
                    if 'district' in value else None)
        name_key = (_canonical_name(name), _canonical_name(district or ''))
        if name_key in mine_names:
            raise PublicationError(
                f'{label} has duplicate normalized mine name/district {name!r}')
        mine_names.add(name_key)
        out_evidence = []
        rows = _expect_list(value['evidence'], f'{mine_label}.evidence')
        if not rows:
            raise PublicationError(f'{mine_label}.evidence must not be empty')
        for evidence_index, evidence in enumerate(rows):
            evidence_label = f'{mine_label}.evidence[{evidence_index}]'
            _expect_keys(
                evidence,
                ('evidence_id', 'source_id', 'page_cite', 'verbatim_quote',
                 'quote_verbatim', 'page_text_sha256'),
                ('measurements', 'historic_values', 'basis', 'years'),
                evidence_label)
            evidence_id = _identifier(evidence['evidence_id'],
                                      f'{evidence_label}.evidence_id')
            if evidence_id in evidence_ids:
                raise PublicationError(f'{label} has duplicate evidence_id {evidence_id}')
            evidence_ids.add(evidence_id)
            source_id = _identifier(evidence['source_id'],
                                    f'{evidence_label}.source_id')
            if source_id not in sources:
                raise PublicationError(
                    f'{evidence_label} references unknown primary source {source_id!r}')
            used_sources.add(source_id)
            page_cite = _page_evidence(evidence['page_cite'],
                                       f'{evidence_label}.page_cite')
            quote = _text(evidence['verbatim_quote'],
                          f'{evidence_label}.verbatim_quote', minimum=8, maximum=5000)
            if evidence['quote_verbatim'] is not True:
                raise PublicationError(f'{evidence_label}.quote_verbatim must be true')
            page_text_sha = _sha(evidence['page_text_sha256'],
                                 f'{evidence_label}.page_text_sha256')
            signature = (source_id, page_cite, quote)
            if signature in evidence_signatures:
                raise PublicationError(
                    f'{label} repeats one source/page/quote as multiple evidence rows')
            evidence_signatures.add(signature)
            native_rows = evidence.get('measurements', [])
            historic_rows = evidence.get('historic_values', [])
            _expect_list(native_rows, f'{evidence_label}.measurements')
            _expect_list(historic_rows, f'{evidence_label}.historic_values')
            if not native_rows and not historic_rows:
                raise PublicationError(
                    f'{evidence_label} needs a native grade or historic value-per-ton')
            normalized = []
            seen_commodities = set()
            for measurement_index, measurement in enumerate(native_rows):
                measurement_label = (
                    f'{evidence_label}.measurements[{measurement_index}]')
                _expect_keys(measurement, ('commodity', 'value', 'unit'), (),
                             measurement_label)
                commodity = measurement['commodity']
                if commodity not in COMMODITIES:
                    raise PublicationError(
                        f'{measurement_label}.commodity {commodity!r} is unsupported')
                if commodity in seen_commodities:
                    raise PublicationError(
                        f'{evidence_label} repeats commodity {commodity}')
                seen_commodities.add(commodity)
                normalized.append(_normalize_native(
                    commodity, measurement['value'], measurement['unit']))
            for historic_index, historic in enumerate(historic_rows):
                commodity = historic.get('commodity') if isinstance(historic, dict) else None
                if commodity in seen_commodities:
                    raise PublicationError(
                        f'{evidence_label} repeats commodity {commodity} across native '
                        'and historic values')
                row = _normalize_historic(historic, prices, price_config_sha)
                seen_commodities.add(row['commodity'])
                normalized.append(row)
            out = {
                'evidence_id': evidence_id,
                'source_id': source_id,
                'page_cite': page_cite,
                'verbatim_quote': quote,
                'quote_verbatim': True,
                'page_text_sha256': page_text_sha,
                'normalized_measurements': sorted(
                    normalized, key=lambda row: COMMODITIES.index(row['commodity'])),
            }
            for optional in ('basis', 'years'):
                if optional in evidence:
                    out[optional] = _text(evidence[optional],
                                          f'{evidence_label}.{optional}', maximum=500)
            out_evidence.append(out)
        mine = {'mine_id': mine_id, 'name': name, 'evidence': out_evidence}
        if district is not None:
            mine['district'] = district
        if 'county' in value:
            mine['county'] = _text(value['county'], f'{mine_label}.county', maximum=200)
        mines.append(mine)
    if set(sources) != used_sources:
        unused = sorted(set(sources) - used_sources)
        raise PublicationError(
            f'{label} declares unused sources that could inflate metrics: {unused}')
    metrics = {
        'graded_mines': len(mines),
        'primary_sources': len(used_sources),
        'verbatim_quotes': len(evidence_ids),
        'page_cites': len(evidence_ids),
        'primary_source_ids': sorted(used_sources),
    }
    return {'sources': [sources[key] for key in sorted(sources)],
            'mines': mines, 'metrics': metrics}


def validate_pp610_document(document, code):
    label = f'{code} PP610 evidence'
    _expect_keys(document,
                 ('schema_version', 'state', 'complete', 'source', 'districts'),
                 ('no_district_finding',), label)
    if (document['schema_version'] != 1 or document['state'] != code or
            document['complete'] is not True):
        raise PublicationError(f'{label} must be schema 1, state-bound, and complete')
    source = _source_identity(document['source'], f'{label}.source',
                              expected_id='pp610')
    districts = []
    district_ids = set()
    names = set()
    signatures = set()
    for index, value in enumerate(_expect_list(document['districts'],
                                                f'{label}.districts')):
        row_label = f'{label}.districts[{index}]'
        _expect_keys(value,
                     ('district_id', 'name', 'page_cite', 'verbatim_quote',
                      'quote_verbatim', 'page_text_sha256'), (), row_label)
        district_id = _identifier(value['district_id'], f'{row_label}.district_id')
        if district_id in district_ids:
            raise PublicationError(f'{label} has duplicate district_id {district_id}')
        district_ids.add(district_id)
        name = _text(value['name'], f'{row_label}.name', minimum=2, maximum=300)
        name_key = _canonical_name(name)
        if name_key in names:
            raise PublicationError(f'{label} has duplicate district name {name!r}')
        names.add(name_key)
        page_cite = _page_evidence(value['page_cite'], f'{row_label}.page_cite')
        quote = _text(value['verbatim_quote'], f'{row_label}.verbatim_quote',
                      minimum=8, maximum=5000)
        if value['quote_verbatim'] is not True:
            raise PublicationError(f'{row_label}.quote_verbatim must be true')
        signature = (page_cite, quote)
        if signature in signatures:
            raise PublicationError(f'{label} repeats a PP610 page/quote')
        signatures.add(signature)
        districts.append({
            'district_id': district_id,
            'name': name,
            'page_cite': page_cite,
            'verbatim_quote': quote,
            'quote_verbatim': True,
            'page_text_sha256': _sha(value['page_text_sha256'],
                                     f'{row_label}.page_text_sha256'),
        })
    no_district = document.get('no_district_finding')
    if districts and no_district is not None:
        raise PublicationError(
            f'{label} cannot declare no_district_finding when districts exist')
    if not districts:
        no_label = f'{label}.no_district_finding'
        _expect_keys(no_district,
                     ('finding', 'pages_reviewed', 'review_complete'), (), no_label)
        finding = _text(no_district['finding'], f'{no_label}.finding',
                        minimum=40, maximum=3000)
        pages = _expect_list(no_district['pages_reviewed'],
                             f'{no_label}.pages_reviewed')
        if (not pages or any(not isinstance(page, str) for page in pages) or
                len(pages) != len(set(pages))):
            raise PublicationError(
                f'{no_label}.pages_reviewed must be a nonempty unique text list')
        pages = [_page_evidence(page, f'{no_label}.pages_reviewed') for page in pages]
        if no_district['review_complete'] is not True:
            raise PublicationError(f'{no_label}.review_complete must be true')
        no_district = {
            'finding': finding,
            'pages_reviewed': pages,
            'review_complete': True,
        }
    return {
        'source': source,
        'complete': True,
        'district_count': len(districts),
        'districts': districts,
        'no_district_finding': no_district,
    }


def validate_low_endowment_document(document, code):
    label = f'{code} low-endowment finding'
    _expect_keys(document,
                 ('schema_version', 'state', 'finding', 'review_complete', 'sources'),
                 (), label)
    if (document['schema_version'] != 1 or document['state'] != code or
            document['review_complete'] is not True):
        raise PublicationError(f'{label} must be schema 1, state-bound, and complete')
    finding = _text(document['finding'], f'{label}.finding',
                    minimum=40, maximum=4000)
    rows = _expect_list(document['sources'], f'{label}.sources')
    if len(rows) < 2:
        raise PublicationError(f'{label} needs at least two primary sources')
    sources = []
    ids = set()
    signatures = set()
    for index, value in enumerate(rows):
        source_label = f'{label}.sources[{index}]'
        _expect_keys(
            value,
            ('source_id', 'title', 'authority', 'url', 'primary',
             'document_sha256', 'page_index_sha256', 'page_cite',
             'verbatim_quote', 'quote_verbatim', 'page_text_sha256'),
            ('citation', 'publication_year'), source_label)
        identity_keys = {
            key: value[key] for key in value
            if key not in ('page_cite', 'verbatim_quote', 'quote_verbatim',
                           'page_text_sha256')
        }
        source = _source_identity(identity_keys, source_label)
        if source['source_id'] in ids:
            raise PublicationError(
                f'{label} has duplicate source_id {source["source_id"]}')
        ids.add(source['source_id'])
        page_cite = _page_evidence(value['page_cite'],
                                   f'{source_label}.page_cite')
        quote = _text(value['verbatim_quote'],
                      f'{source_label}.verbatim_quote', minimum=8, maximum=5000)
        if value['quote_verbatim'] is not True:
            raise PublicationError(f'{source_label}.quote_verbatim must be true')
        signature = (source['document_sha256'], page_cite, quote)
        if signature in signatures:
            raise PublicationError(f'{label} repeats one source/page/quote')
        signatures.add(signature)
        source.update({
            'page_cite': page_cite,
            'verbatim_quote': quote,
            'quote_verbatim': True,
            'page_text_sha256': _sha(value['page_text_sha256'],
                                     f'{source_label}.page_text_sha256'),
        })
        sources.append(source)
    return {
        'finding': finding,
        'review_complete': True,
        'sources': sorted(sources, key=lambda row: row['source_id']),
    }


def _grade_requirement(metrics, low_endowment):
    quantitative = (
        metrics['graded_mines'] >= 25 and metrics['primary_sources'] >= 2 and
        metrics['verbatim_quotes'] >= metrics['graded_mines'] and
        metrics['page_cites'] >= metrics['graded_mines']
    )
    if quantitative:
        return {'status': 'meets_quantitative_bar', 'done_gate_eligible': True,
                'gaps': []}
    if low_endowment is not None:
        return {'status': 'documented_low_endowment', 'done_gate_eligible': True,
                'gaps': []}
    gaps = []
    if metrics['graded_mines'] < 25:
        gaps.append(f'graded_mines {metrics["graded_mines"]}/25')
    if metrics['primary_sources'] < 2:
        gaps.append(f'primary_sources {metrics["primary_sources"]}/2')
    if metrics['verbatim_quotes'] < metrics['graded_mines']:
        gaps.append('verbatim quote count is below graded-mine count')
    if metrics['page_cites'] < metrics['graded_mines']:
        gaps.append('page-cite count is below graded-mine count')
    gaps.append('no reviewed two-primary-source low-endowment finding')
    return {'status': 'incomplete', 'done_gate_eligible': False, 'gaps': gaps}


def _validate_normalized_measurement(value, label, price_config_sha):
    """Recheck a compiled measurement without trusting compiler-era metrics."""
    required = ('commodity', 'value', 'unit', 'input_value', 'input_unit', 'method')
    _expect_keys(value, required, ('price',), label)
    commodity = value['commodity']
    if commodity not in COMMODITIES:
        raise PublicationError(f'{label}.commodity {commodity!r} is unsupported')
    normalized = _number(value['value'], f'{label}.value', positive=True)
    input_value = _number(value['input_value'], f'{label}.input_value', positive=True)
    if value['unit'] != CANONICAL_GRADE_UNITS[commodity]:
        raise PublicationError(f'{label}.unit is not canonical for {commodity}')
    input_unit = value['input_unit']
    method = value['method']
    price = value.get('price')
    if price is None:
        if input_unit not in NATIVE_UNITS[commodity]:
            raise PublicationError(f'{label}.input_unit is invalid for {commodity}')
        conversions = {
            CANONICAL_GRADE_UNITS[commodity]: ('identity', input_value),
            'grams_per_metric_tonne': (
                'grams_per_metric_tonne_x_0.0291666666667',
                input_value * GRAMS_PER_METRIC_TONNE_TO_TROY_OZ_PER_SHORT_TON),
            'parts_per_million': ('parts_per_million_div_10000',
                                  input_value / 10_000),
            'pounds_per_short_ton': ('pounds_per_short_ton_div_20',
                                     input_value / 20),
        }
        expected_method, expected_value = conversions[input_unit]
    else:
        _expect_keys(price, ('year', 'value', 'unit', 'source_id',
                             'price_config_sha256'), (), f'{label}.price')
        if input_unit != 'nominal_usd_per_short_ton':
            raise PublicationError(f'{label}.input_unit is invalid for historic value')
        year = price['year']
        if (not isinstance(year, int) or isinstance(year, bool) or
                not 1800 <= year <= 2100):
            raise PublicationError(f'{label}.price.year is invalid')
        price_value = _number(price['value'], f'{label}.price.value', positive=True)
        if (price['unit'] != PRICE_UNITS[commodity] or
                _identifier(price['source_id'], f'{label}.price.source_id') !=
                price['source_id'] or
                _sha(price['price_config_sha256'],
                     f'{label}.price.price_config_sha256') != price_config_sha):
            raise PublicationError(f'{label}.price provenance is invalid')
        if commodity in ('Au', 'Ag'):
            expected_method = 'usd_per_short_ton_div_usd_per_troy_ounce'
            expected_value = input_value / price_value
        else:
            expected_method = (
                'usd_per_short_ton_div_20lb_per_one_percent_div_usd_per_lb')
            expected_value = input_value / (20 * price_value)
    if method != expected_method or not math.isclose(
            normalized, round(expected_value, 10), rel_tol=1e-12, abs_tol=1e-12):
        raise PublicationError(f'{label} value/method does not reproduce its input')
    return commodity


def _validate_compiled_mines(mines, sources, price_config_sha, code):
    mines = _expect_list(mines, f'{code} compiled mines')
    mine_ids = set()
    mine_names = set()
    evidence_ids = set()
    evidence_signatures = set()
    used_sources = set()
    quote_count = 0
    for mine_index, mine in enumerate(mines):
        label = f'{code} compiled mines[{mine_index}]'
        _expect_keys(mine, ('mine_id', 'name', 'evidence'),
                     ('district', 'county'), label)
        mine_id = _identifier(mine['mine_id'], f'{label}.mine_id')
        if mine_id in mine_ids:
            raise PublicationError(f'{code} compiled evidence has duplicate mine_id {mine_id}')
        mine_ids.add(mine_id)
        name = _text(mine['name'], f'{label}.name', minimum=2, maximum=300)
        district = (_text(mine['district'], f'{label}.district', maximum=300)
                    if 'district' in mine else '')
        name_key = (_canonical_name(name), _canonical_name(district))
        if name_key in mine_names:
            raise PublicationError(
                f'{code} compiled evidence has duplicate normalized mine/district')
        mine_names.add(name_key)
        if 'county' in mine:
            _text(mine['county'], f'{label}.county', maximum=200)
        rows = _expect_list(mine['evidence'], f'{label}.evidence')
        if not rows:
            raise PublicationError(f'{label}.evidence must not be empty')
        for evidence_index, row in enumerate(rows):
            row_label = f'{label}.evidence[{evidence_index}]'
            _expect_keys(
                row,
                ('evidence_id', 'source_id', 'page_cite', 'verbatim_quote',
                 'quote_verbatim', 'page_text_sha256', 'normalized_measurements'),
                ('basis', 'years'), row_label)
            evidence_id = _identifier(row['evidence_id'], f'{row_label}.evidence_id')
            if evidence_id in evidence_ids:
                raise PublicationError(
                    f'{code} compiled evidence has duplicate evidence_id {evidence_id}')
            evidence_ids.add(evidence_id)
            source_id = _identifier(row['source_id'], f'{row_label}.source_id')
            if source_id not in sources:
                raise PublicationError(f'{row_label} references undeclared source {source_id}')
            page = _page_evidence(row['page_cite'], f'{row_label}.page_cite')
            quote = _text(row['verbatim_quote'], f'{row_label}.verbatim_quote',
                          minimum=8, maximum=5000)
            if row['quote_verbatim'] is not True:
                raise PublicationError(f'{row_label}.quote_verbatim must be true')
            _sha(row['page_text_sha256'], f'{row_label}.page_text_sha256')
            signature = (source_id, page, quote)
            if signature in evidence_signatures:
                raise PublicationError(
                    f'{code} compiled evidence repeats a source/page/quote')
            evidence_signatures.add(signature)
            measurements = _expect_list(
                row['normalized_measurements'], f'{row_label}.normalized_measurements')
            if not measurements:
                raise PublicationError(f'{row_label} has no normalized measurements')
            commodities = [
                _validate_normalized_measurement(
                    measurement, f'{row_label}.normalized_measurements[{index}]',
                    price_config_sha)
                for index, measurement in enumerate(measurements)
            ]
            if (len(commodities) != len(set(commodities)) or
                    commodities != sorted(commodities, key=COMMODITIES.index)):
                raise PublicationError(
                    f'{row_label} commodities must be unique and canonical-order')
            for optional in ('basis', 'years'):
                if optional in row:
                    _text(row[optional], f'{row_label}.{optional}', maximum=500)
            used_sources.add(source_id)
            quote_count += 1
    return {
        'graded_mines': len(mine_ids),
        'primary_sources': len(used_sources),
        'verbatim_quotes': quote_count,
        'page_cites': quote_count,
        'primary_source_ids': sorted(used_sources),
    }


def validate_compiled_state_document(document, code, *, registry_row=None,
                                     registry_sha256=None):
    """Validate one immutable state output as a release-consumable artifact.

    This intentionally derives every count from the evidence rows.  A valid
    content hash around a self-consistent but forged metrics object is not a
    DONE-gate proof.
    """
    label = f'{code} compiled grade evidence'
    _expect_keys(
        document,
        ('schema_version', 'dataset', 'snapshot', 'state', 'state_name', 'regime',
         'registry_sha256', 'price_config_sha256', 'input_artifacts', 'metrics',
         'grade_requirement', 'primary_sources', 'mines',
         'low_endowment_finding', 'pp610', 'effect'), (), label)
    if document['schema_version'] != 1 or document['dataset'] != DATASET:
        raise PublicationError(f'{label} dataset/schema identity is invalid')
    if document['state'] != code:
        raise PublicationError(f'{label} state identity is invalid')
    _date(document['snapshot'], f'{label}.snapshot')
    if document['effect'] != 'evidence_only_no_release_mutation':
        raise PublicationError(f'{label}.effect is invalid')
    if registry_row is None or registry_sha256 is None:
        registry, current_sha = _registry_context()
        registry_row = registry.get(code)
        registry_sha256 = current_sha
    if (not isinstance(registry_row, dict) or
            document['state_name'] != registry_row.get('name') or
            document['regime'] != registry_row.get('regime') or
            document['registry_sha256'] != registry_sha256):
        raise PublicationError(f'{label} does not bind the current registry identity')
    price_sha = _sha(document['price_config_sha256'],
                     f'{label}.price_config_sha256')

    input_artifacts = _expect_object(document['input_artifacts'],
                                     f'{label}.input_artifacts')
    low = document['low_endowment_finding']
    expected_inputs = {'grades', 'pp610'} | ({'low_endowment'} if low is not None else set())
    if set(input_artifacts) != expected_inputs:
        raise PublicationError(f'{label}.input_artifacts do not match its evidence')
    for artifact_id, descriptor in input_artifacts.items():
        item_label = f'{label}.input_artifacts.{artifact_id}'
        _expect_keys(descriptor, ('path', 'bytes', 'sha256'), (), item_label)
        path = _text(descriptor['path'], f'{item_label}.path', maximum=500)
        if (os.path.isabs(path) or
                any(part in ('', '.', '..')
                    for part in path.replace('\\', '/').split('/')) or
                not isinstance(descriptor['bytes'], int) or
                isinstance(descriptor['bytes'], bool) or descriptor['bytes'] <= 0):
            raise PublicationError(f'{item_label} path/bytes are invalid')
        _sha(descriptor['sha256'], f'{item_label}.sha256')

    source_rows = _expect_list(document['primary_sources'],
                               f'{label}.primary_sources')
    sources = {}
    for index, source in enumerate(source_rows):
        normalized = _source_identity(source, f'{label}.primary_sources[{index}]')
        if normalized != source:
            raise PublicationError(f'{label}.primary_sources[{index}] is not canonical')
        if normalized['source_id'] in sources:
            raise PublicationError(
                f'{label} has duplicate source_id {normalized["source_id"]}')
        sources[normalized['source_id']] = normalized
    if [row['source_id'] for row in source_rows] != sorted(sources):
        raise PublicationError(f'{label}.primary_sources are not canonical-order')

    computed = _validate_compiled_mines(
        document['mines'], sources, price_sha, code)
    if set(sources) != set(computed['primary_source_ids']):
        raise PublicationError(f'{label} declares unused primary sources')

    if low is not None:
        low_document = dict(low, schema_version=1, state=code)
        normalized_low = validate_low_endowment_document(low_document, code)
        if normalized_low != low:
            raise PublicationError(f'{label}.low_endowment_finding is not canonical')

    pp610 = _expect_object(document['pp610'], f'{label}.pp610')
    _expect_keys(pp610,
                 ('source', 'complete', 'district_count', 'districts',
                  'no_district_finding'), (), f'{label}.pp610')
    pp610_document = {
        'schema_version': 1,
        'state': code,
        'complete': pp610['complete'],
        'source': pp610['source'],
        'districts': pp610['districts'],
        'no_district_finding': pp610['no_district_finding'],
    }
    normalized_pp610 = validate_pp610_document(pp610_document, code)
    if normalized_pp610 != pp610:
        raise PublicationError(f'{label}.pp610 is not canonical')

    computed['pp610_districts'] = pp610['district_count']
    metrics = document['metrics']
    if (not isinstance(metrics, dict) or
            set(metrics) != {'graded_mines', 'primary_sources', 'verbatim_quotes',
                             'page_cites', 'primary_source_ids', 'pp610_districts'} or
            metrics != computed):
        raise PublicationError(f'{label}.metrics do not reconcile with its evidence rows')
    expected_requirement = _grade_requirement(computed, low)
    if document['grade_requirement'] != expected_requirement:
        raise PublicationError(f'{label}.grade_requirement is not derived from evidence')
    return {'metrics': computed, 'grade_requirement': expected_requirement,
            'low_endowment_finding': low, 'pp610': pp610,
            'primary_sources': source_rows}


def _registry_context():
    result = validate_registry()
    if not result.get('ok'):
        raise PublicationError(
            'state registry is invalid: ' + '; '.join(result.get('errors') or []))
    states = load_states()
    rows = {
        code: {
            'name': row['name'],
            'regime': row['regime'],
            'phase': row['phase'],
        }
        for code, row in states.items()
    }
    return rows, sha256_bytes(canonical_bytes(rows))


def load_inventory(inventory_path):
    inventory_path = os.path.realpath(inventory_path)
    _assert_private(inventory_path, 'grade inventory')
    inventory, inventory_raw = load_strict_json(inventory_path, 'grade inventory')
    _expect_keys(inventory,
                 ('schema_version', 'dataset', 'snapshot', 'price_config', 'states'),
                 (), 'grade inventory')
    if inventory['schema_version'] != 1 or inventory['dataset'] != DATASET:
        raise PublicationError(
            f'grade inventory must be schema 1 and dataset={DATASET!r}')
    snapshot = _date(inventory['snapshot'], 'grade inventory.snapshot')
    registry_rows, registry_sha = _registry_context()
    states = _expect_object(inventory['states'], 'grade inventory.states')
    if set(states) != set(registry_rows):
        missing = sorted(set(registry_rows) - set(states))
        extra = sorted(set(states) - set(registry_rows))
        raise PublicationError(
            f'grade inventory must contain exact registry 49: missing={missing}, '
            f'unknown={extra}')
    staging_root = os.path.dirname(inventory_path)
    price_artifact = _artifact_descriptor(
        inventory['price_config'], staging_root, 'price config')
    state_artifacts = {}
    for code in sorted(registry_rows):
        descriptor = states[code]
        label = f'grade inventory.states.{code}'
        _expect_keys(descriptor, ('grades', 'pp610'), ('low_endowment',), label)
        artifacts = {
            'grades': _artifact_descriptor(descriptor['grades'], staging_root,
                                           f'{code}.grades'),
            'pp610': _artifact_descriptor(descriptor['pp610'], staging_root,
                                          f'{code}.pp610'),
        }
        if 'low_endowment' in descriptor:
            artifacts['low_endowment'] = _artifact_descriptor(
                descriptor['low_endowment'], staging_root,
                f'{code}.low_endowment')
        state_artifacts[code] = artifacts
    return {
        'inventory_path': inventory_path,
        'inventory_bytes': len(inventory_raw),
        'inventory_sha256': sha256_bytes(inventory_raw),
        'snapshot': snapshot,
        'registry': registry_rows,
        'registry_sha256': registry_sha,
        'price_artifact': price_artifact,
        'state_artifacts': state_artifacts,
    }


def _verify_unchanged(context):
    path = context['inventory_path']
    if (os.path.getsize(path) != context['inventory_bytes'] or
            sha256_file(path) != context['inventory_sha256']):
        raise PublicationError('grade inventory changed during build')
    artifacts = [context['price_artifact']]
    for state in context['state_artifacts'].values():
        artifacts.extend(state.values())
    for artifact in artifacts:
        try:
            size = os.path.getsize(artifact.path)
            digest = sha256_file(artifact.path)
        except OSError as exc:
            raise PublicationError(
                f'{artifact.label} changed/disappeared during build: {exc}') from exc
        if size != artifact.bytes or digest != artifact.sha256:
            raise PublicationError(f'{artifact.label} changed during build')


def _atomic_write(path, raw):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix='.' + os.path.basename(path) + '.', suffix='.tmp',
        dir=os.path.dirname(path))
    try:
        os.fchmod(descriptor, 0o644)
        with os.fdopen(descriptor, 'wb') as target:
            target.write(raw)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _install_immutable(path, raw):
    if os.path.exists(path):
        with open(path, 'rb') as existing:
            if existing.read() != raw:
                raise PublicationError(
                    f'content-addressed path collision at {path}')
        return
    _atomic_write(path, raw)


def _relative_json_path(value, label):
    if (not isinstance(value, str) or not value.endswith('.json') or
            os.path.isabs(value) or '..' in value.replace('\\', '/').split('/')):
        raise PublicationError(f'{label} must be a safe relative JSON path')
    return value.replace('\\', '/')


def validate_pointer(publish_dir):
    pointer_path = os.path.join(publish_dir, 'latest.json')
    pointer, raw = load_strict_json(pointer_path, 'grade latest pointer')
    _expect_keys(pointer,
                 ('schema_version', 'dataset', 'snapshot', 'run', 'sha256',
                  'states', 'all_states_done_gate_eligible', 'effect'), (),
                 'grade latest pointer')
    if (pointer['schema_version'] != 1 or pointer['dataset'] != DATASET or
            pointer['states'] != 49 or
            pointer['effect'] != 'evidence_only_no_release_mutation'):
        raise PublicationError('grade latest pointer identity is invalid')
    run_ref = _relative_json_path(pointer['run'], 'grade latest pointer.run')
    run_sha = _sha(pointer['sha256'], 'grade latest pointer.sha256')
    run_path = os.path.join(publish_dir, run_ref)
    if sha256_file(run_path) != run_sha:
        raise PublicationError('grade latest pointer run checksum mismatch')
    run, _ = load_strict_json(run_path, 'grade run')
    if run.get('schema_version') != 1 or run.get('dataset') != DATASET:
        raise PublicationError('grade run identity is invalid')
    state_refs = run.get('state_evidence')
    registry_rows, expected_registry_sha = _registry_context()
    if (not isinstance(state_refs, dict) or
            set(state_refs) != set(registry_rows) or
            run.get('registry_sha256') != expected_registry_sha):
        raise PublicationError('grade run does not reference exact 49-state evidence')
    for code, descriptor in state_refs.items():
        _expect_keys(descriptor, ('file', 'bytes', 'sha256', 'metrics',
                                  'done_gate_eligible'), (),
                     f'grade run.state_evidence.{code}')
        artifact = os.path.join(
            publish_dir,
            _relative_json_path(descriptor['file'],
                                f'grade run.state_evidence.{code}.file'))
        if (os.path.getsize(artifact) != descriptor['bytes'] or
                sha256_file(artifact) != _sha(
                    descriptor['sha256'],
                    f'grade run.state_evidence.{code}.sha256')):
            raise PublicationError(f'{code} grade evidence checksum mismatch')
        state_document, state_raw = load_strict_json(artifact, f'{code} grade evidence')
        if state_raw != canonical_bytes(state_document):
            raise PublicationError(f'{code} grade evidence is not canonical JSON')
        checked_state = validate_compiled_state_document(
            state_document, code, registry_row=registry_rows[code],
            registry_sha256=expected_registry_sha)
        if (state_document.get('schema_version') != 1 or
                state_document.get('dataset') != DATASET or
                state_document.get('state') != code or
                state_document.get('registry_sha256') != expected_registry_sha or
                checked_state['metrics'] != descriptor['metrics'] or
                state_document.get('grade_requirement', {}).get(
                    'done_gate_eligible') != descriptor['done_gate_eligible']):
            raise PublicationError(
                f'{code} grade evidence identity/metrics do not match its run descriptor')
    return {'pointer': pointer, 'pointer_bytes': len(raw), 'run': run}


def build(inventory_path, publish_dir, require_done=False, before_commit=None):
    """Compile, verify, and atomically point at one national evidence run."""
    context = load_inventory(inventory_path)
    publish_dir = os.path.realpath(publish_dir)
    if _inside(publish_dir, os.path.dirname(context['inventory_path'])):
        raise PublicationError('publication directory must be outside private staging')
    price_document, _ = load_strict_json(
        context['price_artifact'].path, 'reviewed price config')
    prices = validate_price_config(price_document)
    price_sha = context['price_artifact'].sha256
    state_documents = {}
    state_refs = {}
    incomplete = []
    national_metrics = {
        'graded_mines': 0,
        'primary_sources_sum': 0,
        'verbatim_quotes': 0,
        'page_cites': 0,
        'pp610_districts': 0,
    }
    for code in sorted(context['registry']):
        artifacts = context['state_artifacts'][code]
        grade_raw, _ = load_strict_json(artifacts['grades'].path,
                                        f'{code} grade evidence')
        pp610_raw, _ = load_strict_json(artifacts['pp610'].path,
                                        f'{code} PP610 evidence')
        grades = validate_grade_document(grade_raw, code, prices, price_sha)
        pp610 = validate_pp610_document(pp610_raw, code)
        low_endowment = None
        if 'low_endowment' in artifacts:
            low_raw, _ = load_strict_json(
                artifacts['low_endowment'].path, f'{code} low-endowment finding')
            low_endowment = validate_low_endowment_document(low_raw, code)
        requirement = _grade_requirement(grades['metrics'], low_endowment)
        if not requirement['done_gate_eligible']:
            incomplete.append(code)
        metrics = dict(grades['metrics'], pp610_districts=pp610['district_count'])
        state_document = {
            'schema_version': 1,
            'dataset': DATASET,
            'snapshot': context['snapshot'],
            'state': code,
            'state_name': context['registry'][code]['name'],
            'regime': context['registry'][code]['regime'],
            'registry_sha256': context['registry_sha256'],
            'price_config_sha256': price_sha,
            'input_artifacts': {
                name: {'path': artifact.relative_path,
                       'bytes': artifact.bytes, 'sha256': artifact.sha256}
                for name, artifact in sorted(artifacts.items())
            },
            'metrics': metrics,
            'grade_requirement': requirement,
            'primary_sources': grades['sources'],
            'mines': grades['mines'],
            'low_endowment_finding': low_endowment,
            'pp610': pp610,
            'effect': 'evidence_only_no_release_mutation',
        }
        validate_compiled_state_document(
            state_document, code, registry_row=context['registry'][code],
            registry_sha256=context['registry_sha256'])
        raw = canonical_bytes(state_document)
        digest = sha256_bytes(raw)
        relative = f'states/{code.lower()}/{digest}.json'
        state_documents[code] = (state_document, raw, digest, relative)
        state_refs[code] = {
            'file': relative,
            'bytes': len(raw),
            'sha256': digest,
            'metrics': metrics,
            'done_gate_eligible': requirement['done_gate_eligible'],
        }
        national_metrics['graded_mines'] += metrics['graded_mines']
        national_metrics['primary_sources_sum'] += metrics['primary_sources']
        national_metrics['verbatim_quotes'] += metrics['verbatim_quotes']
        national_metrics['page_cites'] += metrics['page_cites']
        national_metrics['pp610_districts'] += metrics['pp610_districts']
    if require_done and incomplete:
        raise PublicationError(
            'DONE-gate evidence is incomplete for states: ' + ', '.join(incomplete))
    run_document = {
        'schema_version': 1,
        'dataset': DATASET,
        'snapshot': context['snapshot'],
        'states': 49,
        'registry_sha256': context['registry_sha256'],
        'inventory': {
            'bytes': context['inventory_bytes'],
            'sha256': context['inventory_sha256'],
        },
        'price_config': {
            'bytes': context['price_artifact'].bytes,
            'sha256': price_sha,
            'reviewed_on': prices['reviewed_on'],
            'commodities': list(COMMODITIES),
        },
        'national_metrics': national_metrics,
        'done_gate_eligible_states': sorted(set(context['registry']) - set(incomplete)),
        'incomplete_states': incomplete,
        'all_states_done_gate_eligible': not incomplete,
        'state_evidence': state_refs,
        'effect': 'evidence_only_no_release_mutation',
    }
    run_raw = canonical_bytes(run_document)
    run_sha = sha256_bytes(run_raw)
    run_relative = f'runs/{run_sha}.json'
    pointer = {
        'schema_version': 1,
        'dataset': DATASET,
        'snapshot': context['snapshot'],
        'run': run_relative,
        'sha256': run_sha,
        'states': 49,
        'all_states_done_gate_eligible': not incomplete,
        'effect': 'evidence_only_no_release_mutation',
    }
    if before_commit is not None:
        before_commit(context)
    _verify_unchanged(context)
    for code in sorted(state_documents):
        _document, raw, _digest, relative = state_documents[code]
        _install_immutable(os.path.join(publish_dir, relative), raw)
    _install_immutable(os.path.join(publish_dir, run_relative), run_raw)
    _verify_unchanged(context)
    _atomic_write(os.path.join(publish_dir, 'latest.json'), canonical_bytes(pointer))
    checked = validate_pointer(publish_dir)
    if checked['pointer'] != pointer or checked['run'] != run_document:
        raise PublicationError('post-publication grade pointer verification disagrees')
    return {
        'pointer': 'latest.json',
        'run': run_relative,
        'run_sha256': run_sha,
        'states': 49,
        'done_gate_eligible_states': len(run_document['done_gate_eligible_states']),
        'incomplete_states': incomplete,
        'national_metrics': national_metrics,
        'effect': 'evidence_only_no_release_mutation',
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inventory', required=True,
                        help='private checksum inventory (must be outside site/)')
    parser.add_argument('--publish', required=True,
                        help='evidence-only JSON publication directory')
    parser.add_argument('--require-done', action='store_true',
                        help='fail if any state lacks 25/2 evidence or finding')
    args = parser.parse_args(argv)
    try:
        result = build(args.inventory, args.publish,
                       require_done=args.require_done)
    except PublicationError as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == '__main__':
    sys.exit(main())
