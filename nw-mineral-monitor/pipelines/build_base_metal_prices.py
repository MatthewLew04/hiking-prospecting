#!/usr/bin/env python3
"""Build reviewed Cu/Pb/Zn annual price inputs from USGS Data Series 140.

DS 140 reports nominal annual ``Unit value ($/t)`` for apparent U.S.
consumption.  Grade normalization needs dollars per pound, so this builder
performs the single documented conversion (1 metric ton = 2204.62262185 lb).
The reviewed workbook checksums are pinned: a silent upstream replacement
fails instead of changing historical grade conversions unnoticed.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import tempfile
import urllib.request
import zipfile
from xml.etree import ElementTree


HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, 'config', 'base_metal_prices.json')
KG_PER_METRIC_TON = 1000
LB_PER_KG = 2.20462262185
LB_PER_METRIC_TON = KG_PER_METRIC_TON * LB_PER_KG
NS = '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'
CELL_COLUMN = re.compile(r'^([A-Z]+)')

SOURCES = {
    'Cu': {
        'landing_page': 'https://www.usgs.gov/media/files/copper-historical-statistics-data-series-140',
        'workbook_url': ('https://d9-wret.s3.us-west-2.amazonaws.com/assets/palladium/'
                         'production/s3fs-public/media/files/ds140-copper-2020.xlsx'),
        'sha256': '8c5e3682ceae94a06146c4b5000c4499521b02fcbd7dda5954e9ac4eb17ffc3f',
        'last_year': 2020,
    },
    'Pb': {
        'landing_page': 'https://www.usgs.gov/media/files/lead-historical-statistics-data-series-140',
        'workbook_url': ('https://d9-wret.s3.us-west-2.amazonaws.com/assets/palladium/'
                         'production/s3fs-public/media/files/ds140-lead-2021.xlsx'),
        'sha256': '4e896ac3d43d77c1cd0700afbbbd16575a0ab81ee22edce0f833fd46a0b63332',
        'last_year': 2021,
    },
    'Zn': {
        'landing_page': 'https://www.usgs.gov/media/files/zinc-historical-statistics-data-series-140',
        'workbook_url': ('https://d9-wret.s3.us-west-2.amazonaws.com/assets/palladium/'
                         'production/s3fs-public/media/files/ds140-zinc-2022.xlsx'),
        'sha256': 'c84015afb4dfeda24c039fe436a6c288d38eca7eed7cb4caeb09a616dd9d2424',
        'last_year': 2022,
    },
}


def _download(url):
    request = urllib.request.Request(url, headers={
        'User-Agent': 'nw-mineral-monitor/1.0 (USGS public-domain data build)',
    })
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read()


def _shared_strings(bundle):
    if 'xl/sharedStrings.xml' not in bundle.namelist():
        return []
    root = ElementTree.fromstring(bundle.read('xl/sharedStrings.xml'))
    return [''.join(node.text or '' for node in item.iter(NS + 't'))
            for item in root.findall(NS + 'si')]


def _worksheet_rows(raw):
    """Return first-sheet cells as ``[{column: scalar text}, ...]``."""
    with zipfile.ZipFile(io.BytesIO(raw)) as bundle:
        strings = _shared_strings(bundle)
        root = ElementTree.fromstring(bundle.read('xl/worksheets/sheet1.xml'))
    rows = []
    for row in root.iter(NS + 'row'):
        values = {}
        for cell in row.findall(NS + 'c'):
            match = CELL_COLUMN.match(cell.attrib.get('r', ''))
            if not match:
                raise ValueError('DS 140 workbook contains a cell without an A1 column')
            value_node = cell.find(NS + 'v')
            value = value_node.text if value_node is not None else None
            if cell.attrib.get('t') == 's' and value is not None:
                value = strings[int(value)]
            elif cell.attrib.get('t') == 'inlineStr':
                inline = cell.find(NS + 'is')
                value = (''.join(node.text or '' for node in inline.iter(NS + 't'))
                         if inline is not None else None)
            values[match.group(1)] = value
        rows.append(values)
    return rows


def parse_prices(raw, expected_last_year):
    rows = _worksheet_rows(raw)
    header_index = next((index for index, row in enumerate(rows)
                         if 'Year' in row.values() and
                         'Unit value ($/t)' in row.values()), None)
    if header_index is None:
        raise ValueError('DS 140 workbook lacks Year / Unit value ($/t) headers')
    header = rows[header_index]
    year_column = next(column for column, value in header.items() if value == 'Year')
    value_column = next(column for column, value in header.items()
                        if value == 'Unit value ($/t)')
    prices = {}
    for row in rows[header_index + 1:]:
        try:
            year = int(row.get(year_column, ''))
        except (TypeError, ValueError):
            continue
        raw_value = row.get(value_column)
        try:
            value_per_ton = float(raw_value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f'DS 140 year {year} has invalid nominal unit value '
                             f'{raw_value!r}') from exc
        if value_per_ton <= 0:
            raise ValueError(f'DS 140 year {year} has nonpositive unit value')
        prices[str(year)] = round(value_per_ton / LB_PER_METRIC_TON, 8)
    expected_years = list(range(1900, expected_last_year + 1))
    actual_years = sorted(map(int, prices))
    if actual_years != expected_years:
        missing = sorted(set(expected_years) - set(actual_years))
        extra = sorted(set(actual_years) - set(expected_years))
        raise ValueError(f'DS 140 annual series mismatch: missing={missing}, extra={extra}')
    return prices


def build(fetch=_download):
    prices = {}
    provenance = {}
    for metal, source in SOURCES.items():
        raw = fetch(source['workbook_url'])
        digest = hashlib.sha256(raw).hexdigest()
        if digest != source['sha256']:
            raise ValueError(f'{metal} DS 140 workbook checksum changed: {digest}; '
                             'review the replacement before updating SOURCES')
        prices[metal] = parse_prices(raw, source['last_year'])
        provenance[metal] = dict(source, bytes=len(raw), first_year=1900,
                                 source_column='Unit value ($/t)')
    document = {
        'schema_version': 1,
        'status': 'reviewed',
        'units': 'nominal U.S. dollars per pound',
        'basis': ('USGS DS 140 annual nominal unit value of apparent U.S. '
                  'consumption; not an inflation-adjusted or spot-market price'),
        'conversion': {
            'input_units': 'nominal U.S. dollars per metric ton',
            'pounds_per_metric_ton': LB_PER_METRIC_TON,
            'formula': 'usd_per_lb = ds140_unit_value_usd_per_metric_ton / 2204.62262185',
        },
        'source': {
            'title': ('Historical Statistics for Mineral and Material '
                      'Commodities in the United States, Data Series 140'),
            'authority': 'U.S. Geological Survey',
            'public_domain': True,
            'workbooks': provenance,
        },
        'prices_usd_per_lb': prices,
    }
    encoded = (json.dumps(document, indent=2, sort_keys=False,
                          allow_nan=False) + '\n').encode()
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.base-metal-prices-',
                                     suffix='.json', dir=os.path.dirname(OUT))
    try:
        os.fchmod(fd, 0o644)
        with os.fdopen(fd, 'wb') as target:
            target.write(encoded)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, OUT)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    print(json.dumps({metal: {'years': len(values),
                              'through': max(map(int, values))}
                      for metal, values in prices.items()}))
    return document


if __name__ == '__main__':
    build()
