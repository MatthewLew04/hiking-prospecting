#!/usr/bin/env python3
"""Fetch Alaska's state-law mining-location layers into private staging.

This is deliberately not a browser JSON publisher. `--out` must point outside
site/; a later tile build normalizes these rows and emits PMTiles. Federal AK
claims continue to come from MLRS under a distinct `federal_mlrs` namespace.
"""
from __future__ import annotations

import argparse
import json
import os
import stat
import sys
import tempfile

from arcgis_snapshot import capture_layer, canonical_sha256
from common import TODAY

EP = ('https://arcgis.dnr.alaska.gov/arcgis/rest/services/OpenData/'
      'NaturalResource_StateMiningClaim/MapServer')
LAYERS = {'active': 0, 'pending': 1, 'closed': 2}
FIELD_TYPES = {
    'OBJECTID': 'esriFieldTypeOID',
    'CASE_ID': 'esriFieldTypeString',
    'CLAIM_NAME': 'esriFieldTypeString',
    'CSSTTSDSCR': 'esriFieldTypeString',
    'NTPSTDT': 'esriFieldTypeDate',
    'DATE_ALF': 'esriFieldTypeDate',
    'RFRNCMTRSC': 'esriFieldTypeString',
    'NUM_MTR': 'esriFieldTypeString',
    'NMSCTNS': 'esriFieldTypeString',
    'TOT_ACRES': 'esriFieldTypeString',
    'FILENUMBER': 'esriFieldTypeString',
    'INFO_LINK': 'esriFieldTypeString',
    'RFRSHDT': 'esriFieldTypeDate',
}
FIELDS = tuple(FIELD_TYPES)
LAYER_NAMES = {
    'active': 'State Mining Claim Active',
    'pending': 'State Mining Claim Pending',
    'closed': 'State Mining Claim Closed',
}


def _progress(status):
    next_report = [10_000]

    def report(done, total):
        if done >= next_report[0] or done == total:
            print(f'{status}: {done:,}/{total:,}', flush=True)
            next_report[0] = ((done // 10_000) + 1) * 10_000
    return report


def pull(status):
    layer_url = f'{EP}/{LAYERS[status]}'
    features, evidence = capture_layer(
        layer_url,
        expected_name=LAYER_NAMES[status],
        expected_geometry='esriGeometryPolygon',
        required_fields=FIELD_TYPES,
        out_fields=FIELDS,
        page=500,
        # Five decimals collapsed 25 narrow source polygons into zero-area
        # rings in the previous reviewed snapshot. Preserve the eighth digit.
        geometry_precision=8,
        progress=_progress(status),
        verify_progress=_progress(f'{status} verify'),
    )
    rows = []
    for feature in features:
        at = feature.get('attributes') or {}
        serial = at.get('CASE_ID')
        rows.append({
            'source_objectid': at.get('OBJECTID'),
            'claim_key': f'alaska_state_claims:{serial}',
            'system_id': 'alaska_state_claims',
            'jurisdiction': 'state',
            'serial': serial,
            'adl': serial,
            'name': at.get('CLAIM_NAME'),
            'status': status,
            'source_status': at.get('CSSTTSDSCR'),
            'posting_date': at.get('NTPSTDT'),
            'annual_labor_filed': at.get('DATE_ALF'),
            'acres': at.get('TOT_ACRES'),
            'mtrsc': at.get('RFRNCMTRSC'),
            'meridian_township_range': at.get('NUM_MTR'),
            'sections': at.get('NMSCTNS'),
            'file_number': at.get('FILENUMBER'),
            'refresh_date': at.get('RFRSHDT'),
            'info_link': at.get('INFO_LINK'),
            'geometry': feature.get('geometry'),
        })
    if canonical_sha256(features) != evidence['records_sha256']:
        raise RuntimeError(f'{status}: transformed source rows changed in memory')
    return rows, evidence


def _private_output(path):
    real = os.path.realpath(path)
    site = os.path.realpath(os.path.join(os.path.dirname(__file__), '..', 'site'))
    if os.path.commonpath((real, site)) == site:
        raise ValueError(
            'raw statewide Alaska claims are staging-only; emit PMTiles for browser use')
    if os.path.lexists(path) and os.path.islink(path):
        raise ValueError('Alaska claims staging output must not be a symlink')
    return real


def _atomic_json(path, value):
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    mode = stat.S_IMODE(os.stat(path).st_mode) if os.path.exists(path) else 0o600
    handle, pending = tempfile.mkstemp(prefix='.ak-claims-', dir=directory)
    try:
        os.fchmod(handle, mode)
        with os.fdopen(handle, 'w', encoding='utf-8') as output:
            json.dump(value, output, separators=(',', ':'), ensure_ascii=False,
                      allow_nan=False)
            output.flush()
            os.fsync(output.fileno())
        os.replace(pending, path)
    except BaseException:
        try:
            os.close(handle)
        except OSError:
            pass
        try:
            os.unlink(pending)
        except FileNotFoundError:
            pass
        raise


def run(out_path):
    real = _private_output(out_path)
    layers = {}
    evidence = {}
    for status in LAYERS:
        layers[status], evidence[status] = pull(status)
    out = {
        'state': 'AK',
        'system_id': 'alaska_state_claims',
        'retrieved': TODAY,
        'source': EP,
        'snapshot_contract': 'arcgis-objectids-double-pass-v1',
        'source_inventory': evidence,
        'layers': layers,
    }
    _atomic_json(real, out)
    print(f'wrote staging {real}: ' + ', '.join(
        f'{status}={len(rows):,}' for status, rows in out['layers'].items()))
    return out


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', required=True, help='private staging JSON outside site/')
    args = ap.parse_args()
    try:
        run(args.out)
    except (ValueError, RuntimeError) as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        sys.exit(1)
