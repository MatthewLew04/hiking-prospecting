#!/usr/bin/env python3
"""Build checksum-bound South Dakota WS11 grade and PP 610 evidence.

This state-only producer verifies two official federal grade publications and
the official PP 610 anchor.  All historic grade rows are bound to deterministic
300-dpi page images.  OCR and imperfect table text are diagnostics only; the
reviewed image SHA-256 is the transcription boundary.

The producer writes private national-compiler inputs only.  It cannot mutate a
state registry, DONE gate, release, coverage file, manifest, or public asset.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import http.cookiejar
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
DEFAULT_SOURCES = ROOT / 'pipelines/config/sd_grade_sources.json'
DEFAULT_REVIEWED = ROOT / 'grades-research/sd/reviewed_grade_evidence.json'
DEFAULT_DISTRICTS = ROOT / 'grades-research/sd/pp610_district_inventory.json'
DEFAULT_OUTPUT = ROOT / 'build-inputs/ws9/sd-grade-evidence'
OFFICIAL_HOSTS = frozenset(('digital.library.unt.edu', 'pubs.usgs.gov'))
EXPECTED_SOURCE_IDS = frozenset(('usbm-b427', 'usgs-b1332a', 'pp610'))
EXPECTED_GRADE_SOURCE_IDS = EXPECTED_SOURCE_IDS - {'pp610'}
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
    'verbatim_quote', 'quote_verbatim', 'measurements', 'derivation', 'basis',
    'years'))
SHA_RE = re.compile(r'[0-9a-f]{64}')
ID_RE = re.compile(r'[a-z0-9][a-z0-9_.:-]{0,127}')
PP610_FIGURE_PAGE = 239
PP610_DISTRICTS = (
    ('sd-deadwood-two-bit', 'Deadwood-Two Bit', 'Lawrence', 241,
     'p. 235', 'DEADWOOD-TWO BIT DISTRICT'),
    ('sd-lead', 'Lead', 'Lawrence', 243, 'p. 237', 'LEAD DISTRICT'),
    ('sd-garden', 'Garden', 'Lawrence', 242, 'p. 236', 'GARDEN DISTRICT'),
    ('sd-bald-mountain', 'Bald Mountain', 'Lawrence', 241,
     'p. 235', 'BALD MOUNTAIN DISTRICT'),
    ('sd-squaw-creek', 'Squaw Creek', 'Lawrence', 244,
     'p. 238', 'SQUAW CREEK DISTRICT'),
    ('sd-hill-city', 'Hill City', 'Pennington', 245,
     'p. 239', 'HILL CITY DISTRICT'),
    ('sd-keystone', 'Keystone', 'Pennington', 245,
     'p. 239', 'KEYSTONE DISTRICT'),
)
EXPECTED_MINES = (
    'sd-clover-leaf', 'sd-deadbroke', 'sd-mascot',
    'sd-black-diamond-gold-mountain', 'sd-ragged-top-mines', 'sd-cutting',
    'sd-golden-summit', 'sd-keystone', 'sd-lucky-boy', 'sd-big-hit',
    'sd-inca', 'sd-old-bill', 'sd-rough-rider', 'sd-saginaw', 'sd-echo',
    'sd-newark', 'sd-gold-fish', 'sd-hard-scrabble', 'sd-oneonta',
    'sd-minnie-may', 'sd-standby', 'sd-cochrane', 'sd-golden-west',
    'sd-king-of-the-west', 'sd-montana', 'sd-sec12-quartz-vein',
)


class SouthDakotaEvidenceError(ValueError):
    """A South Dakota source or reviewed row violates its fixed contract."""


def canonical_bytes(value):
    try:
        return json.dumps(value, sort_keys=True, separators=(',', ':'),
                          ensure_ascii=False, allow_nan=False).encode('utf-8')
    except (TypeError, ValueError) as exc:
        raise SouthDakotaEvidenceError(
            f'value is not canonical JSON: {exc}') from exc


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
        raise SouthDakotaEvidenceError(str(exc)) from exc


def expect_keys(value, required, optional, label):
    if not isinstance(value, dict):
        raise SouthDakotaEvidenceError(f'{label} must be an object')
    missing = sorted(set(required) - set(value))
    extra = sorted(set(value) - set(required) - set(optional))
    if missing or extra:
        raise SouthDakotaEvidenceError(
            f'{label} keys mismatch: missing={missing}, extra={extra}')


def text(value, label, minimum=1, maximum=5000):
    if (not isinstance(value, str) or value != value.strip() or
            not minimum <= len(value) <= maximum or '\x00' in value):
        raise SouthDakotaEvidenceError(
            f'{label} must be trimmed text of length {minimum}..{maximum}')
    return value


def identifier(value, label):
    text(value, label, maximum=128)
    if ID_RE.fullmatch(value) is None:
        raise SouthDakotaEvidenceError(
            f'{label} must be a lowercase stable identifier')
    return value


def sha(value, label):
    if not isinstance(value, str) or SHA_RE.fullmatch(value) is None:
        raise SouthDakotaEvidenceError(f'{label} must be a lowercase SHA-256')
    return value


def positive_number(value, label):
    if (not isinstance(value, (int, float)) or isinstance(value, bool) or
            not math.isfinite(value) or value <= 0):
        raise SouthDakotaEvidenceError(
            f'{label} must be a positive finite number')
    return float(value)


def is_inside(path, parent):
    try:
        return os.path.commonpath((str(Path(path).resolve()),
                                   str(Path(parent).resolve()))) == str(Path(parent).resolve())
    except (OSError, ValueError):
        return False


def official_url(value, label):
    text(value, label, maximum=2048)
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme != 'https' or parsed.hostname not in OFFICIAL_HOSTS:
        raise SouthDakotaEvidenceError(
            f'{label} must use HTTPS on an approved official archive host')
    if parsed.username or parsed.password or parsed.fragment:
        raise SouthDakotaEvidenceError(
            f'{label} contains forbidden URL components')
    return value


def validate_source_inventory(document):
    expect_keys(document, ('schema_version', 'dataset', 'sources'), (),
                'South Dakota source inventory')
    if (document['schema_version'] != 1 or
            document['dataset'] != 'ws11-south-dakota-grade-source-inventory'):
        raise SouthDakotaEvidenceError(
            'South Dakota source inventory identity is invalid')
    rows = document['sources']
    if not isinstance(rows, list) or not rows:
        raise SouthDakotaEvidenceError(
            'South Dakota source inventory sources must be nonempty')
    sources = {}
    for index, row in enumerate(rows):
        label = f'South Dakota source inventory.sources[{index}]'
        expect_keys(row, SOURCE_KEYS, (), label)
        source_id = identifier(row['source_id'], f'{label}.source_id')
        if source_id in sources:
            raise SouthDakotaEvidenceError(f'duplicate source_id {source_id}')
        for field in ('title', 'authority', 'citation'):
            text(row[field], f'{label}.{field}', minimum=3, maximum=1000)
        year = row['publication_year']
        if (not isinstance(year, int) or isinstance(year, bool) or
                not 1800 <= year <= 2100):
            raise SouthDakotaEvidenceError(
                f'{label}.publication_year is invalid')
        official_url(row['catalog_url'], f'{label}.catalog_url')
        official_url(row['document_url'], f'{label}.document_url')
        local = Path(text(row['local_path'], f'{label}.local_path', maximum=500))
        if local.is_absolute() or '..' in local.parts or '.' in local.parts:
            raise SouthDakotaEvidenceError(
                f'{label}.local_path must be normalized and relative')
        resolved = (ROOT / local).resolve()
        if not is_inside(resolved, ROOT / 'pipelines/cache/sd-grade-sources'):
            raise SouthDakotaEvidenceError(
                f'{label}.local_path must stay in pipelines/cache/sd-grade-sources')
        if (not isinstance(row['bytes'], int) or isinstance(row['bytes'], bool)
                or row['bytes'] <= 0):
            raise SouthDakotaEvidenceError(
                f'{label}.bytes must be a positive integer')
        sha(row['sha256'], f'{label}.sha256')
        if (not isinstance(row['pages'], int) or isinstance(row['pages'], bool)
                or row['pages'] <= 0):
            raise SouthDakotaEvidenceError(
                f'{label}.pages must be a positive integer')
        if row['text_mode'] not in ('embedded', 'ocr'):
            raise SouthDakotaEvidenceError(
                f'{label}.text_mode must be embedded or ocr')
        sources[source_id] = dict(row, resolved_path=resolved)
    if set(sources) != EXPECTED_SOURCE_IDS:
        raise SouthDakotaEvidenceError(
            'South Dakota source inventory must contain exactly usbm-b427, '
            'usgs-b1332a, and pp610')
    if sources['usbm-b427']['text_mode'] != 'ocr':
        raise SouthDakotaEvidenceError('USBM Bulletin 427 must use OCR mode')
    if any(sources[source_id]['text_mode'] != 'embedded'
           for source_id in ('usgs-b1332a', 'pp610')):
        raise SouthDakotaEvidenceError(
            'USGS Bulletin 1332-A and PP 610 must use embedded text mode')
    return sources


def _validate_measurements(evidence, label):
    rows = evidence['measurements']
    if not isinstance(rows, list) or not rows:
        raise SouthDakotaEvidenceError(f'{label}.measurements must be nonempty')
    commodities = set()
    checked = []
    for index, row in enumerate(rows):
        row_label = f'{label}.measurements[{index}]'
        expect_keys(row, ('commodity', 'value', 'unit'), (), row_label)
        commodity = row['commodity']
        if commodity not in national.COMMODITIES or commodity in commodities:
            raise SouthDakotaEvidenceError(
                f'{row_label}.commodity is unsupported or duplicated')
        commodities.add(commodity)
        positive_number(row['value'], f'{row_label}.value')
        if row['unit'] not in national.NATIVE_UNITS[commodity]:
            raise SouthDakotaEvidenceError(f'{row_label}.unit is invalid')
        checked.append(dict(row))
    return checked


def _validate_derivation(evidence, source_id, label):
    derivation = evidence['derivation']
    if not isinstance(derivation, dict):
        raise SouthDakotaEvidenceError(f'{label}.derivation must be an object')
    method = derivation.get('method')
    measurements = evidence['measurements']
    if method == 'native_source_measurement':
        expect_keys(derivation, ('method',), (), f'{label}.derivation')
        if source_id != 'usbm-b427':
            raise SouthDakotaEvidenceError(
                f'{label}: native method is only reviewed for USBM Bulletin 427')
    elif method == 'usd_per_short_ton_div_quoted_usd_per_troy_ounce':
        expect_keys(derivation,
                    ('method', 'usd_per_short_ton', 'usd_per_troy_ounce'), (),
                    f'{label}.derivation')
        if source_id != 'usbm-b427' or len(measurements) != 1 or not (
                measurements[0]['commodity'] == 'Au' and
                measurements[0]['unit'] == 'troy_ounces_per_short_ton'):
            raise SouthDakotaEvidenceError(
                f'{label}: dollar conversion must be one USBM B427 Au row')
        value_per_ton = positive_number(
            derivation['usd_per_short_ton'],
            f'{label}.derivation.usd_per_short_ton')
        price = positive_number(
            derivation['usd_per_troy_ounce'],
            f'{label}.derivation.usd_per_troy_ounce')
        expected = round(value_per_ton / price, 10)
        if measurements[0]['value'] != expected:
            raise SouthDakotaEvidenceError(
                f'{label}: converted Au value must equal round(value/price, 10)')
        quoted_dollars = [float(value) for value in re.findall(
            r'\$\s*(\d+(?:\.\d+)?)', evidence['verbatim_quote'])]
        for operand, operand_label in ((value_per_ton, 'value per ton'),
                                       (price, 'gold price')):
            if not any(math.isclose(operand, quoted, rel_tol=0, abs_tol=1e-9)
                       for quoted in quoted_dollars):
                raise SouthDakotaEvidenceError(
                    f'{label}: quotation does not contain derivation {operand_label}')
    elif method == 'parts_per_million_as_native_units':
        expect_keys(derivation, ('method', 'parts_per_million'), (),
                    f'{label}.derivation')
        if source_id != 'usgs-b1332a':
            raise SouthDakotaEvidenceError(
                f'{label}: ppm method is only reviewed for USGS Bulletin 1332-A')
        ppm = derivation['parts_per_million']
        if not isinstance(ppm, dict) or set(ppm) != {
                row['commodity'] for row in measurements}:
            raise SouthDakotaEvidenceError(
                f'{label}.derivation.parts_per_million must match commodities')
        for row in measurements:
            value = positive_number(ppm[row['commodity']],
                                    f'{label}.derivation.parts_per_million')
            expected_unit = ('grams_per_metric_tonne'
                             if row['commodity'] in ('Au', 'Ag')
                             else 'parts_per_million')
            if row['unit'] != expected_unit or row['value'] != value:
                raise SouthDakotaEvidenceError(
                    f'{label}: 1 ppm must remain the reviewed native-unit value')
    else:
        raise SouthDakotaEvidenceError(
            f'{label}.derivation.method is not an approved South Dakota method')


def validate_reviewed(document, sources):
    expect_keys(document, REVIEW_KEYS, (), 'reviewed South Dakota grade evidence')
    if (document['schema_version'] != 1 or document['state'] != 'SD' or
            document['status'] != 'reviewed' or
            document['dataset'] != 'ws11-south-dakota-reviewed-grade-evidence'):
        raise SouthDakotaEvidenceError(
            'reviewed South Dakota evidence identity/status is invalid')
    for field in ('reviewed_on', 'reviewed_by', 'review_method'):
        text(document[field], f'reviewed South Dakota evidence.{field}', minimum=3)
    mines = document['mines']
    if not isinstance(mines, list) or tuple(
            row.get('mine_id') if isinstance(row, dict) else None
            for row in mines) != EXPECTED_MINES:
        raise SouthDakotaEvidenceError(
            'reviewed South Dakota evidence must contain the exact ordered '
            '26-target review set')
    evidence_ids = set()
    mine_names = set()
    signatures = set()
    source_counts = {source_id: 0 for source_id in EXPECTED_GRADE_SOURCE_IDS}
    checked_mines = []
    for mine_index, mine in enumerate(mines):
        label = f'reviewed South Dakota evidence.mines[{mine_index}]'
        expect_keys(mine, MINE_KEYS, (), label)
        identifier(mine['mine_id'], f'{label}.mine_id')
        for field in ('name', 'district', 'county'):
            text(mine[field], f'{label}.{field}', minimum=2, maximum=300)
        name_key = (
            re.sub(r'[^a-z0-9]+', ' ', mine['name'].lower()).strip(),
            re.sub(r'[^a-z0-9]+', ' ', mine['district'].lower()).strip())
        if name_key in mine_names:
            raise SouthDakotaEvidenceError(
                f'duplicate normalized mine/district {mine["name"]!r}')
        mine_names.add(name_key)
        if not isinstance(mine['evidence'], list) or len(mine['evidence']) != 1:
            raise SouthDakotaEvidenceError(
                f'{label}.evidence must contain exactly one reviewed row')
        evidence = mine['evidence'][0]
        ev_label = f'{label}.evidence[0]'
        expect_keys(evidence, EVIDENCE_KEYS, (), ev_label)
        evidence_id = identifier(evidence['evidence_id'],
                                 f'{ev_label}.evidence_id')
        if evidence_id in evidence_ids:
            raise SouthDakotaEvidenceError(
                f'duplicate evidence_id {evidence_id}')
        evidence_ids.add(evidence_id)
        source_id = identifier(evidence['source_id'], f'{ev_label}.source_id')
        if source_id not in EXPECTED_GRADE_SOURCE_IDS:
            raise SouthDakotaEvidenceError(
                f'{ev_label} references unknown/non-grade source {source_id}')
        source_counts[source_id] += 1
        page = evidence['pdf_page']
        if (not isinstance(page, int) or isinstance(page, bool) or
                not 1 <= page <= sources[source_id]['pages']):
            raise SouthDakotaEvidenceError(
                f'{ev_label}.pdf_page is outside the source')
        page_cite = text(evidence['page_cite'], f'{ev_label}.page_cite',
                         minimum=2, maximum=200)
        if not any(character.isdigit() for character in page_cite):
            raise SouthDakotaEvidenceError(
                f'{ev_label}.page_cite needs a numbered page')
        quote = text(evidence['verbatim_quote'],
                     f'{ev_label}.verbatim_quote', minimum=8)
        if evidence['quote_verbatim'] is not True:
            raise SouthDakotaEvidenceError(
                f'{ev_label}.quote_verbatim must be true')
        sha(evidence['page_image_sha256'],
            f'{ev_label}.page_image_sha256')
        signature = (source_id, page_cite, quote)
        if signature in signatures:
            raise SouthDakotaEvidenceError(
                f'{ev_label} repeats a source/page/quote signature')
        signatures.add(signature)
        checked_evidence = dict(evidence)
        checked_evidence['measurements'] = _validate_measurements(
            evidence, ev_label)
        _validate_derivation(checked_evidence, source_id, ev_label)
        for field in ('basis', 'years'):
            text(evidence[field], f'{ev_label}.{field}', maximum=500)
        checked_mines.append({
            **{key: mine[key]
               for key in ('mine_id', 'name', 'district', 'county')},
            'evidence': [checked_evidence],
        })
    if source_counts != {'usbm-b427': 20, 'usgs-b1332a': 6}:
        raise SouthDakotaEvidenceError(
            f'reviewed source distribution must be 20/6; got {source_counts}')
    return checked_mines, set(source_counts)


def validate_district_inventory(document, sources):
    expect_keys(document,
                ('schema_version', 'dataset', 'state', 'source_id',
                 'review_scope', 'figure_pdf_page',
                 'figure_page_image_sha256', 'districts'), (),
                'South Dakota PP 610 inventory')
    if (document['schema_version'] != 1 or document['state'] != 'SD' or
            document['source_id'] != 'pp610' or
            document['dataset'] !=
            'ws11-south-dakota-pp610-district-inventory'):
        raise SouthDakotaEvidenceError(
            'South Dakota PP 610 inventory identity is invalid')
    text(document['review_scope'],
         'South Dakota PP 610 inventory.review_scope', minimum=40)
    if document['figure_pdf_page'] != PP610_FIGURE_PAGE:
        raise SouthDakotaEvidenceError(
            'South Dakota PP 610 completeness boundary must be Figure 23 PDF page 239')
    figure_sha = sha(document['figure_page_image_sha256'],
                     'South Dakota PP 610 inventory.figure_page_image_sha256')
    rows = document['districts']
    if not isinstance(rows, list) or len(rows) != len(PP610_DISTRICTS):
        raise SouthDakotaEvidenceError(
            'South Dakota PP 610 inventory must contain all seven Figure 23 districts')
    quotes = set()
    checked = []
    required = ('district_id', 'name', 'county', 'pdf_page', 'page_cite',
                'source_heading', 'verbatim_quote', 'quote_verbatim')
    for index, (row, expected) in enumerate(zip(rows, PP610_DISTRICTS)):
        label = f'South Dakota PP 610 inventory.districts[{index}]'
        expect_keys(row, required, (), label)
        actual = tuple(row[key] for key in required[:6])
        if actual != expected:
            raise SouthDakotaEvidenceError(
                f'{label} is not Figure 23 district {index + 1}: expected {expected}')
        identifier(row['district_id'], f'{label}.district_id')
        if not 1 <= row['pdf_page'] <= sources['pp610']['pages']:
            raise SouthDakotaEvidenceError(f'{label}.pdf_page is outside PP 610')
        quote = text(row['verbatim_quote'], f'{label}.verbatim_quote',
                     minimum=8, maximum=500)
        if row['quote_verbatim'] is not True:
            raise SouthDakotaEvidenceError(
                f'{label}.quote_verbatim must be true')
        if quote in quotes:
            raise SouthDakotaEvidenceError(
                f'{label} duplicates a PP 610 locator quote')
        quotes.add(quote)
        checked.append(dict(row))
    return checked, figure_sha


def verify_pdf(source):
    path = source['resolved_path']
    if path.is_symlink():
        raise SouthDakotaEvidenceError(
            f'{source["source_id"]} PDF must not be a symlink')
    try:
        actual_bytes = path.stat().st_size
        actual_sha = sha256_file(path)
    except OSError as exc:
        raise SouthDakotaEvidenceError(
            f'{source["source_id"]} PDF unavailable; run fetch: {exc}') from exc
    if actual_bytes != source['bytes'] or actual_sha != source['sha256']:
        raise SouthDakotaEvidenceError(
            f'{source["source_id"]} source drift: expected '
            f'{source["bytes"]}/{source["sha256"]}, got '
            f'{actual_bytes}/{actual_sha}')
    if not shutil.which('pdfinfo'):
        raise SouthDakotaEvidenceError(
            'pdfinfo is required to verify source page counts')
    process = subprocess.run(['pdfinfo', str(path)], capture_output=True,
                             text=True, check=False)
    if process.returncode:
        raise SouthDakotaEvidenceError(
            f'pdfinfo rejected {source["source_id"]}: '
            f'{process.stderr.strip()}')
    match = re.search(r'^Pages:\s+(\d+)\s*$', process.stdout, re.MULTILINE)
    if match is None or int(match.group(1)) != source['pages']:
        raise SouthDakotaEvidenceError(
            f'{source["source_id"]} page count differs from reviewed inventory')
    try:
        with open(path, 'rb') as pdf:
            magic = pdf.read(5)
    except OSError as exc:
        raise SouthDakotaEvidenceError(
            f'cannot inspect {source["source_id"]}: {exc}') from exc
    if magic != b'%PDF-':
        raise SouthDakotaEvidenceError(
            f'{source["source_id"]} source is not a PDF')


def _parse_altcha(html):
    """Return UNT's proof-of-work form fields, or ``None`` for normal pages."""
    def find(pattern):
        match = re.search(pattern, html, re.IGNORECASE)
        return match.group(1) if match else None

    csrf = find(r'name=["\']csrfmiddlewaretoken["\']\s+value=["\']([^"\']+)')
    fields = {}
    for key in ('algorithm', 'challenge', 'salt', 'signature', 'maxnumber'):
        fields[key] = find(rf'["\']{key}["\']\s*:\s*["\']?([^,"\'\s}}]+)')
    if not csrf or not all(fields.values()):
        return None
    fields['csrfmiddlewaretoken'] = csrf
    return fields


def _solve_altcha(fields):
    if fields['algorithm'].upper() != 'SHA-256':
        raise SouthDakotaEvidenceError(
            f'unsupported UNT ALTCHA algorithm {fields["algorithm"]!r}')
    maximum = int(fields['maxnumber'])
    for number in range(maximum + 1):
        candidate = hashlib.sha256(
            f'{fields["salt"]}{number}'.encode('utf-8')).hexdigest()
        if candidate == fields['challenge']:
            payload = {
                'algorithm': fields['algorithm'],
                'challenge': fields['challenge'],
                'number': number,
                'salt': fields['salt'],
                'signature': fields['signature'],
            }
            return base64.b64encode(canonical_bytes(payload)).decode('ascii')
    raise SouthDakotaEvidenceError(
        'UNT ALTCHA proof was not found inside the declared search bound')


def _open_pinned(opener, url, expected_bytes):
    request = urllib.request.Request(
        url, headers={'User-Agent': 'nw-mineral-monitor-ws11/1'})
    response = opener.open(request, timeout=120)
    final = urllib.parse.urlparse(response.geturl())
    if final.scheme != 'https' or final.hostname not in OFFICIAL_HOSTS:
        response.close()
        raise SouthDakotaEvidenceError(
            'source redirected outside approved official archive hosts')
    raw = response.read(expected_bytes + 1)
    response.close()
    return raw


def fetch_source(source):
    path = source['resolved_path']
    if path.exists():
        verify_pdf(source)
        return 'verified'
    path.parent.mkdir(parents=True, exist_ok=True)
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    raw = _open_pinned(opener, source['document_url'], source['bytes'])
    if not raw.startswith(b'%PDF-') and source['source_id'] == 'usbm-b427':
        fields = _parse_altcha(raw.decode('utf-8', 'replace'))
        if fields is None:
            raise SouthDakotaEvidenceError(
                'UNT returned a non-PDF response without a recognized ALTCHA form')
        post = urllib.parse.urlencode({
            'csrfmiddlewaretoken': fields['csrfmiddlewaretoken'],
            'altcha': _solve_altcha(fields),
        }).encode('ascii')
        request = urllib.request.Request(
            'https://digital.library.unt.edu/dam/submit/', data=post,
            headers={
                'User-Agent': 'nw-mineral-monitor-ws11/1',
                'Referer': source['document_url'],
                'Content-Type': 'application/x-www-form-urlencoded',
            })
        with opener.open(request, timeout=120) as response:
            response.read(1 << 20)
        raw = _open_pinned(opener, source['document_url'], source['bytes'])
    if (len(raw) != source['bytes'] or sha256_bytes(raw) != source['sha256']
            or not raw.startswith(b'%PDF-')):
        raise SouthDakotaEvidenceError(
            f'{source["source_id"]} download does not match reviewed '
            'bytes/SHA-256/PDF magic')
    temp_path = path.with_name(path.name + '.part')
    try:
        with open(temp_path, 'wb') as output:
            output.write(raw)
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink()
    verify_pdf(source)
    return 'downloaded'


def run_tool(command, label):
    process = subprocess.run(command, capture_output=True, check=False)
    if process.returncode:
        raise SouthDakotaEvidenceError(
            f'{label} failed: '
            f'{process.stderr.decode("utf-8", "replace").strip()}')
    return process.stdout


def extract_text_page(source, pdf_page):
    if not shutil.which('pdftotext'):
        raise SouthDakotaEvidenceError(
            'pdftotext is required for cited-page extraction')
    return run_tool([
        'pdftotext', '-f', str(pdf_page), '-l', str(pdf_page), '-enc', 'UTF-8',
        '-layout', str(source['resolved_path']), '-'],
        f'pdftotext {source["source_id"]} page {pdf_page}')


def render_page_image(source, pdf_page):
    if not shutil.which('pdftoppm'):
        raise SouthDakotaEvidenceError(
            'pdftoppm is required for page-image review binding')
    with tempfile.TemporaryDirectory(prefix='sd-grade-page-') as directory:
        base = Path(directory) / 'page'
        run_tool([
            'pdftoppm', '-f', str(pdf_page), '-l', str(pdf_page), '-r', '300',
            '-png', '-singlefile', str(source['resolved_path']), str(base)],
            f'pdftoppm {source["source_id"]} page {pdf_page}')
        image = base.with_suffix('.png')
        if not image.is_file():
            raise SouthDakotaEvidenceError(
                'pdftoppm did not create the reviewed page image')
        return sha256_file(image)


def extract_ocr_page(source, pdf_page):
    for tool in ('pdftoppm', 'tesseract'):
        if not shutil.which(tool):
            raise SouthDakotaEvidenceError(
                f'{tool} is required for USBM Bulletin 427 scan pages')
    with tempfile.TemporaryDirectory(prefix='sd-grade-ocr-') as directory:
        base = Path(directory) / 'page'
        run_tool([
            'pdftoppm', '-f', str(pdf_page), '-l', str(pdf_page), '-r', '300',
            '-png', '-singlefile', str(source['resolved_path']), str(base)],
            f'pdftoppm {source["source_id"]} page {pdf_page}')
        image = base.with_suffix('.png')
        if not image.is_file():
            raise SouthDakotaEvidenceError(
                'pdftoppm did not create the reviewed scan page')
        image_sha = sha256_file(image)
        process = subprocess.run(
            ['tesseract', image.name, 'stdout', '--psm', '6'], cwd=directory,
            capture_output=True, check=False)
        if process.returncode:
            raise SouthDakotaEvidenceError(
                f'tesseract {source["source_id"]} page {pdf_page} failed: '
                f'{process.stderr.decode("utf-8", "replace").strip()}')
        return process.stdout, image_sha


def page_record(source, page, cache, image_bound=False):
    key = (source['source_id'], page)
    if key not in cache:
        if source['text_mode'] == 'ocr':
            raw, image_sha = extract_ocr_page(source, page)
        else:
            raw = extract_text_page(source, page)
            image_sha = None
        cache[key] = {
            'raw': raw,
            'text': raw.decode('utf-8', 'replace'),
            'page_text_sha256': sha256_bytes(raw),
            'page_image_sha256': image_sha,
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
        raise SouthDakotaEvidenceError(
            f'output path must not be a symlink: {path}')
    raw = canonical_bytes(document)
    with tempfile.NamedTemporaryFile(
            dir=path.parent, prefix=path.name + '.', suffix='.part',
            delete=False) as output:
        output.write(raw)
        temp_name = output.name
    os.replace(temp_name, path)
    return {'path': str(path), 'bytes': len(raw), 'sha256': sha256_bytes(raw)}


def tool_version(command):
    if not shutil.which(command):
        return None
    arguments = [command, '--version'] if command == 'tesseract' else [command, '-v']
    process = subprocess.run(arguments, capture_output=True, text=True,
                             check=False)
    lines = (process.stdout or process.stderr).splitlines()
    return lines[0].strip() if lines else 'unknown'


def build(sources_path=DEFAULT_SOURCES, reviewed_path=DEFAULT_REVIEWED,
          districts_path=DEFAULT_DISTRICTS, output=DEFAULT_OUTPUT):
    sources_document, sources_raw = load_json(
        Path(sources_path), 'South Dakota source inventory')
    reviewed_document, reviewed_raw = load_json(
        Path(reviewed_path), 'reviewed South Dakota grade evidence')
    districts_document, districts_raw = load_json(
        Path(districts_path), 'South Dakota PP 610 inventory')
    sources = validate_source_inventory(sources_document)
    mines, grade_source_ids = validate_reviewed(reviewed_document, sources)
    districts, figure_image_sha = validate_district_inventory(
        districts_document, sources)
    for source_id in sorted(sources):
        verify_pdf(sources[source_id])

    output = Path(output).resolve()
    if is_inside(output, SITE):
        raise SouthDakotaEvidenceError(
            'South Dakota raw evidence output must stay outside site/')
    if output == ROOT or not is_inside(output, ROOT):
        raise SouthDakotaEvidenceError(
            'South Dakota raw evidence output must stay inside workspace')

    page_cache = {}
    source_page_rows = {source_id: {} for source_id in sources}
    grade_mines = []
    conversion_count = 0
    ppm_count = 0
    for mine in mines:
        out_mine = {
            key: mine[key] for key in ('mine_id', 'name', 'district', 'county')}
        out_mine['evidence'] = []
        for evidence in mine['evidence']:
            source_id = evidence['source_id']
            source = sources[source_id]
            page = page_record(source, evidence['pdf_page'], page_cache,
                               image_bound=True)
            if page['page_image_sha256'] != evidence['page_image_sha256']:
                raise SouthDakotaEvidenceError(
                    f'{evidence["evidence_id"]}: reviewed grade page-image '
                    'SHA-256 changed')
            score = extraction.quote_match_score(
                evidence['verbatim_quote'], page['text'])
            method = evidence['derivation']['method']
            conversion_count += int(
                method == 'usd_per_short_ton_div_quoted_usd_per_troy_ounce')
            ppm_count += int(method == 'parts_per_million_as_native_units')
            out_mine['evidence'].append({
                'evidence_id': evidence['evidence_id'],
                'source_id': source_id,
                'page_cite': evidence['page_cite'],
                'verbatim_quote': evidence['verbatim_quote'],
                'quote_verbatim': True,
                'page_text_sha256': page['page_text_sha256'],
                'measurements': evidence['measurements'],
                'basis': evidence['basis'],
                'years': evidence['years'],
            })
            page_row = source_page_rows[source_id].setdefault(
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
                'derivation_method': method,
                'review_boundary':
                    'page_image_sha256_human_review_text_match_diagnostic',
            })
        grade_mines.append(out_mine)

    page_index_artifacts = {}
    identities = {}
    for source_id in sorted(grade_source_ids):
        source = sources[source_id]
        page_index = {
            'schema_version': 1,
            'source_id': source_id,
            'document_sha256': source['sha256'],
            'extraction': {
                'text_mode': source['text_mode'],
                'text_arguments': ('tesseract --psm 6'
                                   if source['text_mode'] == 'ocr'
                                   else 'pdftotext -enc UTF-8 -layout'),
                'page_render': 'pdftoppm -r 300 -png',
                'text_role':
                    'diagnostic_only_page_image_sha_is_review_boundary',
            },
            'pages': [source_page_rows[source_id][page]
                      for page in sorted(source_page_rows[source_id])],
        }
        index_sha = sha256_bytes(canonical_bytes(page_index))
        page_index_artifacts[source_id] = atomic_json(
            output / 'page-indexes' / f'{source_id}.{index_sha}.json',
            page_index)
        identities[source_id] = source_identity(source, index_sha)

    grades_document = {
        'schema_version': 1,
        'state': 'SD',
        'sources': [identities[source_id]
                    for source_id in sorted(identities)],
        'mines': grade_mines,
    }
    try:
        grade_validation = national.validate_grade_document(
            grades_document, 'SD', {}, '0' * 64)
    except national.PublicationError as exc:
        raise SouthDakotaEvidenceError(
            f'national grade contract rejected South Dakota: {exc}') from exc
    grades_artifact = atomic_json(output / 'grades/sd.json', grades_document)

    pp_source = sources['pp610']
    figure = page_record(pp_source, PP610_FIGURE_PAGE, page_cache,
                         image_bound=True)
    if figure['page_image_sha256'] != figure_image_sha:
        raise SouthDakotaEvidenceError(
            'PP 610 Figure 23 reviewed page-image SHA-256 changed')
    pp_page_rows = {}
    pp_districts = []
    for district in districts:
        page = page_record(pp_source, district['pdf_page'], page_cache)
        coverage = extraction.quote_word_coverage(
            district['verbatim_quote'], page['text'])
        if coverage < 0.85:
            raise SouthDakotaEvidenceError(
                f'{district["district_id"]}: PP 610 quote/page word coverage '
                f'{coverage:.3f} is below 0.85')
        pp_districts.append({
            'district_id': district['district_id'],
            'name': district['name'],
            'page_cite': district['page_cite'],
            'verbatim_quote': district['verbatim_quote'],
            'quote_verbatim': True,
            'page_text_sha256': page['page_text_sha256'],
        })
        page_row = pp_page_rows.setdefault(district['pdf_page'], {
            'pdf_page': district['pdf_page'],
            'printed_page': district['pdf_page'] - 6,
            'page_text_sha256': page['page_text_sha256'],
            'checks': [],
        })
        page_row['checks'].append({
            'district_id': district['district_id'],
            'source_heading': district['source_heading'],
            'quote_word_coverage': coverage,
            'review_boundary': 'page_text_sha256_and_verbatim_quote',
        })
    pp_index = {
        'schema_version': 1,
        'source_id': 'pp610',
        'document_sha256': pp_source['sha256'],
        'extraction': {
            'text_mode': 'embedded',
            'pdftotext_arguments': '-enc UTF-8 -layout',
            'scope': ('complete South Dakota Figure 23 inventory, with one '
                      'reviewed descriptive chapter quote per numbered district'),
            'figure_23_completeness_page': {
                'pdf_page': PP610_FIGURE_PAGE,
                'printed_page': 233,
                'page_text_sha256': figure['page_text_sha256'],
                'page_image_sha256': figure['page_image_sha256'],
                'page_render': 'pdftoppm -r 300 -png',
            },
        },
        'pages': [pp_page_rows[page] for page in sorted(pp_page_rows)],
    }
    pp_index_sha = sha256_bytes(canonical_bytes(pp_index))
    page_index_artifacts['pp610'] = atomic_json(
        output / 'page-indexes' / f'pp610.{pp_index_sha}.json', pp_index)
    pp_document = {
        'schema_version': 1,
        'state': 'SD',
        'complete': True,
        'source': source_identity(pp_source, pp_index_sha),
        'districts': pp_districts,
    }
    try:
        pp_validation = national.validate_pp610_document(pp_document, 'SD')
    except national.PublicationError as exc:
        raise SouthDakotaEvidenceError(
            f'national PP 610 contract rejected South Dakota: {exc}') from exc
    pp_artifact = atomic_json(output / 'pp610/sd.json', pp_document)

    def relative(path):
        return str(Path(path).resolve().relative_to(output))

    for artifact in [grades_artifact, pp_artifact,
                     *page_index_artifacts.values()]:
        artifact['path'] = relative(artifact['path'])
    image_pages = sum(
        1 for (source_id, _), page in page_cache.items()
        if source_id in grade_source_ids and page['page_image_sha256'])
    report = {
        'schema_version': 1,
        'dataset': 'ws11-south-dakota-grade-evidence-build',
        'state': 'SD',
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
            'figure_23_page_hashes': 1,
            'pp610_description_page_hashes': len(pp_page_rows),
            'grade_image_pages_review_bound': image_pages,
            'explicit_quote_price_conversions': conversion_count,
            'ppm_table_rows': ppm_count,
        },
        'threshold_observation': {
            'at_least_25_graded_mines':
                grade_validation['metrics']['graded_mines'] >= 25,
            'at_least_2_primary_sources':
                grade_validation['metrics']['primary_sources'] >= 2,
            'complete_pp610_anchor': pp_validation['district_count'] == 7,
            'is_release_decision': False,
        },
        'data_limitations': [
            ('USBM Bulletin 427 is an image-only historic scan; OCR is '
             'diagnostic and each used page is bound to its reviewed 300-dpi '
             'PNG SHA-256.'),
            ('USGS Bulletin 1332-A numeric tables have an imperfect text '
             'layer; selected rows are transcribed against reviewed 300-dpi '
             'page images.'),
            ('Seventeen USBM dollar-per-ton statements are converted to gold '
             'ounces per short ton only where the same quotation supplies the '
             'contemporaneous gold price; both operands remain in the private '
             'review record.'),
            ('The 26-target evidence set is concentrated in the documented '
             'Black Hills/Homestake-region endowment and is not a statewide '
             'inventory of every occurrence.'),
        ],
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
    source_document, _ = load_json(
        Path(sources_path), 'South Dakota source inventory')
    reviewed_document, _ = load_json(
        Path(reviewed_path), 'reviewed South Dakota grade evidence')
    district_document, _ = load_json(
        Path(districts_path), 'South Dakota PP 610 inventory')
    sources = validate_source_inventory(source_document)
    mines, used = validate_reviewed(reviewed_document, sources)
    districts, _ = validate_district_inventory(district_document, sources)
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
    commands = result.add_subparsers(dest='command', required=True)
    commands.add_parser('check', help='validate review manifests without PDFs')
    fetch = commands.add_parser('fetch', help='fetch/verify official pinned PDFs')
    fetch.add_argument('--all', action='store_true',
                       help='accepted for parity; all three sources are consumed')
    build_parser = commands.add_parser(
        'build', help='verify pages and produce private compiler inputs')
    build_parser.add_argument('--output', default=str(DEFAULT_OUTPUT))
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command == 'check':
            result = check_inputs(args.sources, args.reviewed, args.districts)
        elif args.command == 'fetch':
            source_document, _ = load_json(
                Path(args.sources), 'South Dakota source inventory')
            sources = validate_source_inventory(source_document)
            result = {source_id: fetch_source(sources[source_id])
                      for source_id in sorted(sources)}
        else:
            report, artifact = build(
                args.sources, args.reviewed, args.districts, args.output)
            result = {'metrics': report['metrics'], 'build': artifact}
    except (SouthDakotaEvidenceError, OSError) as exc:
        print(f'South Dakota grade evidence ERROR: {exc}', file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
