#!/usr/bin/env python3
"""Fetch ARDF as Alaska's occurrence backbone into private tile staging."""
from __future__ import annotations

import argparse
import json
import os
import stat
import sys
import tempfile

from arcgis_snapshot import capture_layer, canonical_sha256
from common import TODAY

EP = ('https://services.arcgis.com/v01gqwM5QqNysAAi/ArcGIS/rest/services/'
      'ARDF_features/FeatureServer')

FIELD_TYPES = {
    'OBJECTID': 'esriFieldTypeOID',
    'Site': 'esriFieldTypeString',
    'Commodities_main': 'esriFieldTypeString',
    'Quad_250': 'esriFieldTypeString',
    'Quad_63360': 'esriFieldTypeString',
    'Latitude': 'esriFieldTypeDouble',
    'Longitude': 'esriFieldTypeDouble',
    'Location': 'esriFieldTypeString',
    'Commodities_other': 'esriFieldTypeString',
    'Ore_minerals': 'esriFieldTypeString',
    'Gangue_minerals': 'esriFieldTypeString',
    'Site_type': 'esriFieldTypeString',
    'Site_status': 'esriFieldTypeString',
    'Production': 'esriFieldTypeString',
    'Generic_model': 'esriFieldTypeString',
    'Deposit_model': 'esriFieldTypeString',
    'Geologic_description': 'esriFieldTypeString',
    'Workings_exploration': 'esriFieldTypeString',
    'ARDF_no': 'esriFieldTypeString',
    'Last_report_date': 'esriFieldTypeString',
    'MRDS_no': 'esriFieldTypeString',
    'Age': 'esriFieldTypeString',
    'Primary_reference': 'esriFieldTypeString',
    'State': 'esriFieldTypeString',
    'District': 'esriFieldTypeString',
    'Host_rock': 'esriFieldTypeString',
    'Host_rock_age': 'esriFieldTypeString',
    'Assoc_ign_rock': 'esriFieldTypeString',
    'Ign_rock_age': 'esriFieldTypeString',
    'Quadrangle': 'esriFieldTypeString',
    'SYMBOL': 'esriFieldTypeString',
}


def _private_output(path):
    real = os.path.realpath(path)
    site = os.path.realpath(os.path.join(os.path.dirname(__file__), '..', 'site'))
    if os.path.commonpath((real, site)) == site:
        raise ValueError('raw statewide ARDF is staging-only; emit PMTiles for browser use')
    if os.path.lexists(path) and os.path.islink(path):
        raise ValueError('ARDF staging output must not be a symlink')
    return real


def _atomic_json(path, value):
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    mode = stat.S_IMODE(os.stat(path).st_mode) if os.path.exists(path) else 0o600
    handle, pending = tempfile.mkstemp(prefix='.ardf-', dir=directory)
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
    next_report = [2_000]

    def progress(done, total):
        if done >= next_report[0] or done == total:
            print(f'ardf: {done:,}/{total:,}', flush=True)
            next_report[0] = ((done // 2_000) + 1) * 2_000

    verify_next_report = [2_000]

    def verify_progress(done, total):
        if done >= verify_next_report[0] or done == total:
            print(f'ardf verify: {done:,}/{total:,}', flush=True)
            verify_next_report[0] = ((done // 2_000) + 1) * 2_000

    source_features, evidence = capture_layer(
        f'{EP}/0',
        expected_name='Alaska Resource Data File',
        expected_geometry='esriGeometryPoint',
        required_fields=FIELD_TYPES,
        out_fields=tuple(FIELD_TYPES),
        page=500,
        geometry_precision=8,
        progress=progress,
        verify_progress=verify_progress,
    )
    rows = [
        {'properties': feature.get('attributes') or {},
         'geometry': feature.get('geometry')}
        for feature in source_features
    ]
    if canonical_sha256(source_features) != evidence['records_sha256']:
        raise RuntimeError('ARDF transformed source rows changed in memory')
    value = {
        'state': 'AK',
        'source_id': 'ardf',
        'retrieved': TODAY,
        'source': f'{EP}/0',
        'snapshot_contract': 'arcgis-objectids-double-pass-v1',
        'source_inventory': evidence,
        'n': len(rows),
        'features': rows,
    }
    _atomic_json(real, value)
    print(f'wrote staging {real}: {len(rows):,} ARDF occurrences')
    return value


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', required=True, help='private staging JSON outside site/')
    args = ap.parse_args()
    try:
        run(args.out)
    except (ValueError, RuntimeError) as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        sys.exit(1)
