#!/usr/bin/env python3
"""Build Alaska state-claim and ARDF browser baselines as PMTiles.

The two inputs are private staging snapshots produced by ``fetch_ak_claims``
and ``fetch_ardf``.  They must live outside ``site/``.  This builder verifies
their snapshot identity, authoritative row counts, feature identities,
geometry shape/ranges, source duplicates, and deterministic feature IDs before
creating either browser archive.  No raw statewide JSON is published.

The 2026-08-13 snapshot intentionally preserves repeated Alaska DNR case rows:
a case can have multiple source geometries, and the service also contains a
small number of byte-identical rows. Those rows are counted explicitly and
receive stable occurrence IDs; they are never silently deduplicated. The
legacy five-decimal snapshot's zero-area rings are rejected: the source must
be refetched at eight-decimal precision, and every resulting source row must
reconcile to a unique maximum-zoom PMTiles feature ID before publication.
Twenty-four valid official polygons are narrower than the z13 MVT grid. They
remain unchanged source polygons in a tiny z19 precision-overflow archive;
the ordinary z13 archive excludes exactly those OBJECTIDs, and the two
inventories must form an exact disjoint union.

After all three archives validate and are atomically installed, the builder
rereads the latest ``manifest.json`` and atomically merges two
``national_baselines`` entries. Private verification must use
``--no-manifest --private-output-dir`` so it cannot replace browser artifacts.
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import gzip
import hashlib
import json
import math
import os
import re
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import time

from validate_national import (
    _decompress_pmtiles,
    _directory_entries,
    _mvt_layers,
    _pmtiles_header as _strict_pmtiles_header,
)


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
SITE = os.path.join(ROOT, 'site')
CLAIMS_OUT = os.path.join(SITE, 'data', 'tiles', 'claims', 'ak-state.pmtiles')
CLAIMS_PRECISION_OUT = os.path.join(
    SITE, 'data', 'tiles', 'claims', 'ak-state-precision.pmtiles')
ARDF_OUT = os.path.join(SITE, 'data', 'tiles', 'national', 'ardf.pmtiles')
MANIFEST = os.path.join(SITE, 'data', 'manifest.json')

CLAIMS_SOURCE = ('https://arcgis.dnr.alaska.gov/arcgis/rest/services/OpenData/'
                 'NaturalResource_StateMiningClaim/MapServer')
ARDF_SOURCE = ('https://services.arcgis.com/v01gqwM5QqNysAAi/ArcGIS/rest/'
               'services/ARDF_features/FeatureServer/0')
SNAPSHOT_DATE = '2026-08-13'
CLAIM_STATUSES = ('active', 'pending', 'closed')
EXPECTED_CLAIM_COUNTS = {'active': 39_269, 'pending': 51, 'closed': 79_480}
EXPECTED_ARDF_COUNT = 7_692
EXPECTED_CLAIMS_STAGING_SHA256 = (
    '559ec5ebe3285b4fce7e5117576c3d23aebdd911bd957ca5cc7ed6f83b4861d9')
EXPECTED_ARDF_STAGING_SHA256 = (
    '9e35dd394f1e1f2702e1d309bf6ab6e49859300ff643dd00d6dd755e1cc95be3')
SNAPSHOT_CONTRACT = 'arcgis-objectids-double-pass-v1'
CLAIMS_MAXZOOM = 13
CLAIMS_PRECISION_MAXZOOM = 19
ARDF_MAXZOOM = 13
PUBLICATION_GRACE_SECONDS = 30
PRECISION_LAYERS = {
    'active': 'active_precision',
    'closed': 'closed_precision',
}
EXPECTED_PRECISION_SOURCE_GEOMETRY_SHA256 = {
    'active': {
        4_387: '60d1d17f5abcd19935b41e0d8e44dce0bfa78d1cecda62fec2cab8888ee1e915',
        28_315: '83afffd05ba27e39acf242f4c067d4042e369648ac3ccb77a784debc3163273b',
        28_336: '0eba77381d275119b2c36bd344d18edc0ad2bececc6235432556137109f09c63',
        28_339: '8317bf06b9cdd7be21e4b2602f25003c0567725b7f04991b917dade4140f27fa',
        29_696: '7645d613407e2619845355e52e757e90290ac757f8b5efaf99ac7ec815a55769',
        34_704: '0db415acc689575f3cdef4a09c69612e1ae7c8ce6fafd961842223e84efdc15f',
    },
    'closed': {
        2_862: '3b459374998c2d7023b046d7ca7480ca7b54b1aefccda4a668beff8817210d4b',
        42_145: 'fe026666d90e8641bca33a1f6e4f428905c14880414e82a4c529f487ae2a596f',
        58_122: '36c641ced9ac7733e2bb865ca14f15c508da96abe53f4f6cf1848ad1a3605f81',
        58_231: '6e7570b91d31ec86699ff51bb7fcf740029b979093563d18b5a54f47b87f2450',
        58_233: 'f073b192b25b590d07b6d3f7692ce9cfea3c279a6c05cb83d2deff168f172f39',
        61_311: 'a8623f238dcfa6a8ec37c7d3ae83960b8e54c4139e2570ed57c783b7dff3feb6',
        64_278: '9223f98695b0eb8f7eeea92193c760abc9505bc318c6cee499a1887b1839ccbd',
        64_745: '42ff841fc30745f6664629f2c5ae96ff763ba3a95c7bd7b3f92a3ba95f72f2d6',
        67_536: '7b7a043539104c95973d937f1a33ebcf8b7ebba3d5b1e39d008ec47e57533ae1',
        67_799: '997b9236399cfa68ab7efcb1a356cd5d7983098d3b2a8b640e6e8f073f0bbc15',
        67_813: 'e062ae6a0bf019924fe13fccbad7e1533d8390c7b428d9ae776cc0fe8f6d7a69',
        67_860: '2d6e0167d5fb48f4c96e5fc7c9baacfdc8c366465010d41398368c451355890d',
        69_440: '8c616c34ef38c7492f44c54d323e3f82eb6ce92f01563cbfe4f1bb7239c0beda',
        69_857: '66824c3dd832ead315ab336ef11afc0ecb7aaaa9b048ec3d31d1e8ed220f7e5e',
        76_315: '626243247c52705b0b57bbef7aea15969ad0a25fdd7c1a0e9af2d360dd203b53',
        76_538: '5acf399fa4a12a1b04f78227ad7280c9d9a2f9a41901c30ee76c50eccd33913a',
        78_830: 'aed914c27887785c9bc915c9eff9a23e6963ea5537ae71f879abd3ae07d5f39d',
        79_329: '81709f4769aa9bcaa68951ddbb9d708f79324fd17bbed79a3a0cccc46a5b8b9a',
    },
}
EXPECTED_CLAIM_SOURCE_INVENTORY = {
    'active': {
        'n': 39_269, 'minimum_object_id': 1, 'maximum_object_id': 39_269,
        'object_ids_sha256':
            '0485c20d51b54e3365ef09862a99bd5ffb84a264d5befaea13125b33cd44c544',
        'layer_metadata_sha256':
            'b2f31d2a0a49b8c300449cebaf4af525892e0d34948c70f72ebfad1d655b1c71',
        'records_sha256':
            'bc4f98f71367ce33332e77e821030fcab4b13fa6319d91f1d82892a6d672f2d1',
    },
    'pending': {
        'n': 51, 'minimum_object_id': 1, 'maximum_object_id': 51,
        'object_ids_sha256':
            'c331e85759e39947a7591a5b52bab252382637ab99ac844cee79867609389c68',
        'layer_metadata_sha256':
            '383da01317d7432300164e444e7af83233fb4a77c779f24ad9f9bbc7bfd735b8',
        'records_sha256':
            '3a50d733a536330dc3821ba97599f69d387cc1f7b2b24cb2fdd4fd70dc1829ec',
    },
    'closed': {
        'n': 79_480, 'minimum_object_id': 1, 'maximum_object_id': 79_480,
        'object_ids_sha256':
            'f8de22d4beb0f032e67914544cec05c496357883405e57bb89ee38e4d2f4fa53',
        'layer_metadata_sha256':
            '543cf76e3b3ab78f286551fbae0f047ea3022684821cea3ae33b351a736cab6f',
        'records_sha256':
            '976e743caf50652927a78b9e5c4971a5dcbeac1e9b34dd8becd623ade0eb3081',
    },
}
EXPECTED_ARDF_SOURCE_INVENTORY = {
    'n': 7_692, 'minimum_object_id': 1, 'maximum_object_id': 7_719,
    'object_ids_sha256':
        '7c5cd4f66b8790294fecad6d9ab0dde10b647cdab9d98f1a5b4e2832a668bb8d',
    'layer_metadata_sha256':
        '0bb304fa62e4e429496c0f25e1fed3bb295680312bbe02809de1d027c938687b',
    'records_sha256':
        'c7501d1e1174a1e5ee8b11bcff651930a11b4ddada73adb02c3157d0773c93bb',
}
# These six reviewed official rows contain an empty Site_type string.  The
# absence is source evidence: it must stay distinguishable from a reported
# occurrence type and must not disappear as a null MVT property.
EXPECTED_ARDF_BLANK_SITE_TYPE_OBJECTIDS = frozenset((
    2828, 3251, 3367, 3662, 5307, 6568,
))
SOURCE_VALUE_MISSING = 'Not reported by source'
SOURCE_VALUE_REPORTED = 'reported'
SOURCE_VALUE_BLANK = 'source_blank'
SAFE_INTEGER_MAX = (1 << 53) - 1
ARDF_NUMBER_RE = re.compile(r'^[A-Z]{2}\d{3}$')
ISO_DATE_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')
NUMERIC_PM_FIELDS = frozenset(('lon', 'lat', 'group', 'ex', 'source_oid'))
ARDF_BROWSER_FIELDS = (
    'st', 'id', 'nm', 'g', 'g_status', 'group', 'ex', 'typ',
    'typ_status', 'status', 'district', 'district_status',
)
CLAIM_REQUIRED = frozenset((
    'source_objectid', 'claim_key', 'system_id', 'jurisdiction', 'serial', 'adl', 'name',
    'status', 'source_status', 'posting_date', 'annual_labor_filed', 'acres',
    'mtrsc', 'meridian_township_range', 'sections', 'file_number',
    'refresh_date', 'info_link', 'geometry',
))
ARDF_REQUIRED = frozenset((
    'OBJECTID', 'Site', 'Commodities_main', 'Quad_250', 'Quad_63360',
    'Latitude', 'Longitude', 'Location', 'Commodities_other', 'Ore_minerals',
    'Gangue_minerals', 'Site_type', 'Site_status', 'Production',
    'Generic_model', 'Deposit_model', 'Geologic_description',
    'Workings_exploration', 'ARDF_no', 'Last_report_date', 'MRDS_no', 'Age',
    'Primary_reference', 'State', 'District', 'Host_rock', 'Host_rock_age',
    'Assoc_ign_rock', 'Ign_rock_age', 'Quadrangle', 'SYMBOL',
))


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_sha256(value):
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
        allow_nan=False).encode('utf-8')).hexdigest()


def _integer_id_inventory(values):
    values = sorted(values)
    if any(isinstance(value, bool) or not isinstance(value, int) or value <= 0
           for value in values) or len(values) != len(set(values)):
        raise ValueError('source identity inventory must contain unique positive integers')
    return {
        'records': len(values),
        'minimum_id': values[0] if values else None,
        'maximum_id': values[-1] if values else None,
        'ids_sha256': _canonical_sha256(values),
    }


def _reviewed_source_inventory(value, expected, label):
    """Require the exact reviewed identity emitted by the snapshot producer."""
    if not isinstance(value, dict):
        raise ValueError(f'{label} source inventory must be an object')
    observed = {key: value.get(key) for key in expected}
    if observed != expected:
        raise ValueError(
            f'{label} source inventory differs from the reviewed snapshot: '
            f'expected={expected}, observed={observed}')
    verification = value.get('verification')
    required_verification = {
        'page_mode': 'exact_object_ids',
        'full_second_feature_pass': True,
        'postflight_metadata_match': True,
        'postflight_object_ids_match': True,
        'geometry_precision': 8,
    }
    if verification != required_verification:
        raise ValueError(
            f'{label} source inventory lacks exact double-pass verification')


def _feature_id(namespace, identity):
    """Return a deterministic positive integer exactly representable in JS."""
    digest = hashlib.blake2b(
        f'{namespace}\x1f{identity}'.encode('utf-8'), digest_size=8,
        person=b'nwmm-ak-v1').digest()
    return (int.from_bytes(digest, 'big') & SAFE_INTEGER_MAX) or 1


def _private_staging_path(path, label):
    real = os.path.realpath(path)
    site = os.path.realpath(SITE)
    try:
        inside_site = os.path.commonpath((real, site)) == site
    except ValueError:
        inside_site = False
    if inside_site:
        raise ValueError(f'{label} staging must remain outside site/')
    if not os.path.isfile(real):
        raise ValueError(f'{label} staging file does not exist: {path}')
    return real


def _private_output_directory(path):
    """Resolve an explicit non-browser destination for reproducibility runs."""
    if not isinstance(path, str) or not path.strip():
        raise ValueError('private output directory must be a nonempty path')
    if os.path.lexists(path) and os.path.islink(path):
        raise ValueError('private output directory must not be a symlink')
    real = os.path.realpath(path)
    site = os.path.realpath(SITE)
    try:
        inside_site = os.path.commonpath((real, site)) == site
    except ValueError:
        inside_site = False
    if inside_site:
        raise ValueError('private output directory must remain outside site/')
    os.makedirs(real, mode=0o700, exist_ok=True)
    if not os.path.isdir(real):
        raise ValueError('private output destination is not a directory')
    return real


def _load_json(path, label):
    try:
        with open(path, encoding='utf-8') as source:
            value = json.load(source)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f'{label} staging is not readable JSON: {exc}') from exc
    if not isinstance(value, dict):
        raise ValueError(f'{label} staging must contain a JSON object')
    return value


def _text(value, label, *, required=False, maximum=500):
    if value is None:
        if required:
            raise ValueError(f'{label} must be a nonempty string')
        return None
    if not isinstance(value, str):
        raise ValueError(f'{label} must be a string or null')
    value = ' '.join(value.split())
    if not value:
        if required:
            raise ValueError(f'{label} must be a nonempty string')
        return None
    if len(value) > maximum:
        raise ValueError(f'{label} exceeds {maximum} characters')
    return value


def _excerpt(value, label, maximum, truncations):
    """Compact optional ARDF prose and count every intentional truncation."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f'{label} must be a string or null')
    value = ' '.join(value.split())
    if not value:
        return None
    if len(value) > maximum:
        truncations[label.rsplit('.', 1)[-1]] += 1
        value = value[:maximum - 1].rstrip() + '…'
    return value


def _finite(value, label, *, minimum, maximum):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f'{label} must be a finite number')
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f'{label} must be a finite number')
    if not minimum <= result <= maximum:
        raise ValueError(f'{label} is outside [{minimum}, {maximum}]')
    return result


def _date_from_epoch(value, label):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f'{label} must be integer epoch milliseconds or null')
    try:
        date = dt.datetime.fromtimestamp(value / 1000, tz=dt.timezone.utc).date()
    except (OverflowError, OSError, ValueError) as exc:
        raise ValueError(f'{label} is invalid epoch milliseconds') from exc
    if not 1900 <= date.year <= 2200:
        raise ValueError(f'{label} resolves outside the supported 1900-2200 range')
    return date.isoformat()


def _acres(value, label):
    if value is None or isinstance(value, bool):
        raise ValueError(f'{label} must be a nonnegative number')
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f'{label} must be a nonnegative number') from exc
    if not math.isfinite(result) or result < 0 or result > 100_000_000:
        raise ValueError(f'{label} must be a nonnegative finite number')
    return result


def _signed_area(ring):
    # Translate near the origin before the shoelace sum. Direct products of
    # ~150-degree longitudes lose the eighth-decimal differences needed for
    # the narrow DNR polygons this builder is specifically required to keep.
    origin_x, origin_y = ring[0]
    return sum(
        (left[0] - origin_x) * (right[1] - origin_y) -
        (right[0] - origin_x) * (left[1] - origin_y)
        for left, right in zip(ring, ring[1:])) / 2


def _point_in_ring(point, ring):
    """Even/odd containment test; boundary counts as enclosed for assignment."""
    x, y = point
    inside = False
    for left, right in zip(ring, ring[1:]):
        x1, y1 = left
        x2, y2 = right
        cross = (x - x1) * (y2 - y1) - (y - y1) * (x2 - x1)
        if abs(cross) <= 1e-12 and min(x1, x2) <= x <= max(x1, x2) and \
                min(y1, y2) <= y <= max(y1, y2):
            return True
        if (y1 > y) != (y2 > y):
            at_x = (x2 - x1) * (y - y1) / (y2 - y1) + x1
            if x <= at_x:
                inside = not inside
    return inside


def _unwrap_ring(ring, anchor=None):
    """Return a dateline-continuous copy of a closed longitude/latitude ring."""
    out = []
    previous = float(anchor) if anchor is not None else None
    for raw_x, raw_y in ring:
        x = float(raw_x)
        if previous is not None:
            while x - previous > 180:
                x -= 360
            while x - previous < -180:
                x += 360
        out.append([x, float(raw_y)])
        previous = x
    return out


def _ring_centroid(ring):
    cross_sum = 0.0
    x_sum = 0.0
    y_sum = 0.0
    for left, right in zip(ring, ring[1:]):
        cross = left[0] * right[1] - right[0] * left[1]
        cross_sum += cross
        x_sum += (left[0] + right[0]) * cross
        y_sum += (left[1] + right[1]) * cross
    if abs(cross_sum) <= 1e-12:
        return None
    return [x_sum / (3 * cross_sum), y_sum / (3 * cross_sum)]


def _representative_point(geometry, label):
    """Choose a stable point on the largest source polygon component.

    The point is computed before tiling, so popups/search/distance queries do
    not inherit an arbitrary tile-clipped vertex. Polygon centroid and bbox
    candidates are accepted only when inside the exterior and outside holes;
    a source-edge midpoint is the deterministic fallback for degenerate rings.
    """
    components = ([geometry['coordinates']] if geometry['type'] == 'Polygon'
                  else geometry['coordinates'])
    ranked = []
    for component in components:
        if not isinstance(component, list) or not component:
            continue
        exterior = _unwrap_ring(component[0])
        holes = [_unwrap_ring(hole, exterior[0][0]) for hole in component[1:]]
        ranked.append((abs(_signed_area(exterior)), exterior, holes))
    if not ranked:
        raise ValueError(f'{label} has no component for representative point')
    _, exterior, holes = max(ranked, key=lambda item: item[0])
    xs = [point[0] for point in exterior[:-1]]
    ys = [point[1] for point in exterior[:-1]]
    candidates = [_ring_centroid(exterior),
                  [(min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2]]
    candidates.extend([[(a[0] + b[0]) / 2, (a[1] + b[1]) / 2]
                       for a, b in zip(exterior, exterior[1:])])
    candidates.extend(exterior[:-1])
    for candidate in candidates:
        if (candidate is not None and _point_in_ring(candidate, exterior) and
                not any(_point_in_ring(candidate, hole) for hole in holes)):
            longitude = ((candidate[0] + 180) % 360) - 180
            return round(longitude, 7), round(candidate[1], 7)
    raise ValueError(f'{label} has no valid representative point')


def _esri_polygon(value, label):
    """Validate Esri rings and return RFC 7946 Polygon/MultiPolygon + facts."""
    if not isinstance(value, dict) or not isinstance(value.get('rings'), list) or \
            not value['rings']:
        raise ValueError(f'{label} must contain nonempty Esri polygon rings')
    rings = []
    areas = []
    source_ring_count = len(value['rings'])
    collapsed_point_rings = 0
    for ring_index, raw_ring in enumerate(value['rings']):
        ring_label = f'{label}.rings[{ring_index}]'
        if not isinstance(raw_ring, list) or len(raw_ring) < 4:
            raise ValueError(f'{ring_label} must contain at least four positions')
        ring = []
        for point_index, raw_point in enumerate(raw_ring):
            point_label = f'{ring_label}[{point_index}]'
            if not isinstance(raw_point, list) or len(raw_point) != 2:
                raise ValueError(f'{point_label} must be a [longitude, latitude] pair')
            longitude = _finite(
                raw_point[0], f'{point_label}[0]', minimum=-180, maximum=180)
            latitude = _finite(
                raw_point[1], f'{point_label}[1]', minimum=50, maximum=73)
            # Match the reviewed fetch contract. Seven decimals can collapse
            # a narrow polygon even when ArcGIS returned a valid eighth digit.
            ring.append([round(longitude, 8), round(latitude, 8)])
        if ring[0] != ring[-1]:
            raise ValueError(f'{ring_label} is not closed')
        if len(set(map(tuple, ring[:-1]))) < 3:
            raise ValueError(
                f'{ring_label} collapsed below three distinct polygon vertices; '
                'refetch at higher geometry precision')
        rings.append(ring)
        areas.append(_signed_area(ring))

    if not rings:
        raise ValueError(f'{label} has no ring with at least two distinct positions')

    # Esri outer rings are clockwise (negative signed area); holes are
    # counter-clockwise. A zero-area claim ring cannot be represented as a
    # polygon without inventing land, so a fresh higher-precision source pull
    # is mandatory rather than silently dropping it during tiling.
    outer_indices = [index for index, area in enumerate(areas) if area < 0]
    hole_indices = [index for index, area in enumerate(areas) if area > 0]
    zero_indices = [index for index, area in enumerate(areas) if area == 0]
    if zero_indices:
        raise ValueError(
            f'{label} contains zero-area rings {zero_indices}; refetch at '
            'higher geometry precision')
    counterclockwise_exterior = 0
    if not outer_indices and hole_indices:
        if len(hole_indices) != 1:
            raise ValueError(f'{label} has multiple counter-clockwise rings and no exterior')
        # The legacy five-decimal snapshot has a handful of extremely narrow
        # single-ring features whose orientation flipped during rounding. With
        # no possible enclosing ring, the sole ring is unambiguously exterior.
        outer_indices = hole_indices
        hole_indices = []
        counterclockwise_exterior = 1
    components = [[
        rings[index] if areas[index] > 0 else list(reversed(rings[index]))
    ] for index in outer_indices]
    component_areas = [abs(areas[index]) for index in outer_indices]
    for index in hole_indices:
        candidates = [
            component for component, outer_index in enumerate(outer_indices)
            if _point_in_ring(rings[index][0], rings[outer_index])
        ]
        if not candidates:
            raise ValueError(f'{label}.rings[{index}] is a hole outside every exterior')
        component = min(candidates, key=lambda candidate: component_areas[candidate])
        components[component].append(list(reversed(rings[index])))
    if not components:
        raise ValueError(f'{label} contains no usable rings')
    geometry = ({'type': 'Polygon', 'coordinates': components[0]}
                if len(components) == 1 else
                {'type': 'MultiPolygon', 'coordinates': components})
    return geometry, {
        'rings': source_ring_count,
        'emitted_rings': len(rings),
        'collapsed_point_rings': collapsed_point_rings,
        'counterclockwise_exteriors': counterclockwise_exterior,
        'zero_area_rings': len(zero_indices),
        'zero_area_feature': int(bool(zero_indices) and len(zero_indices) == len(rings)),
        'parts': len(components),
    }


def _claim_record(row, status, index):
    label = f'claims.layers.{status}[{index}]'
    if not isinstance(row, dict) or not CLAIM_REQUIRED <= set(row):
        missing = sorted(CLAIM_REQUIRED - set(row if isinstance(row, dict) else ()))
        raise ValueError(f'{label} is missing required fields {missing}')
    source_oid = row.get('source_objectid')
    if (isinstance(source_oid, bool) or not isinstance(source_oid, int) or
            source_oid <= 0):
        raise ValueError(f'{label}.source_objectid must be a positive integer')
    serial = _text(row.get('serial'), f'{label}.serial', required=True, maximum=40)
    if not re.fullmatch(r'ADL \d+', serial):
        raise ValueError(f'{label}.serial is not an ADL identifier')
    exact = {
        'claim_key': f'alaska_state_claims:{serial}',
        'system_id': 'alaska_state_claims',
        'jurisdiction': 'state',
        'adl': serial,
        'status': status,
    }
    for field, expected in exact.items():
        if row.get(field) != expected:
            raise ValueError(
                f'{label}.{field} is {row.get(field)!r}, expected {expected!r}')
    source_geometry_sha256 = _canonical_sha256(row.get('geometry'))
    expected_geometry_sha256 = \
        EXPECTED_PRECISION_SOURCE_GEOMETRY_SHA256.get(status, {}).get(source_oid)
    if (expected_geometry_sha256 is not None and
            source_geometry_sha256 != expected_geometry_sha256):
        raise ValueError(
            f'{label}.geometry changed for reviewed precision source OBJECTID '
            f'{source_oid}')
    geometry, geometry_facts = _esri_polygon(row.get('geometry'), f'{label}.geometry')
    longitude, latitude = _representative_point(geometry, f'{label}.geometry')
    info_link = _text(
        row.get('info_link'), f'{label}.info_link', required=True, maximum=500)
    if not info_link.startswith('https://dnr.alaska.gov/projects/las/'):
        raise ValueError(f'{label}.info_link is not an Alaska DNR LAS URL')
    properties = {
        'st': 'AK',
        'system': 'state',
        'serial': serial,
        'nm': _text(row.get('name'), f'{label}.name', maximum=300),
        'status': status,
        'lon': longitude,
        'lat': latitude,
        'source_status': _text(
            row.get('source_status'), f'{label}.source_status', required=True,
            maximum=100),
        'posted': _date_from_epoch(row.get('posting_date'), f'{label}.posting_date'),
        'labor': _date_from_epoch(
            row.get('annual_labor_filed'), f'{label}.annual_labor_filed'),
        'acres': _acres(row.get('acres'), f'{label}.acres'),
        'mtrsc': _text(row.get('mtrsc'), f'{label}.mtrsc', required=True, maximum=80),
        'file': _text(
            row.get('file_number'), f'{label}.file_number', required=True, maximum=60),
        'refreshed': _date_from_epoch(
            row.get('refresh_date'), f'{label}.refresh_date'),
        'url': info_link,
    }
    if geometry_facts['collapsed_point_rings']:
        properties['source_collapsed_rings'] = geometry_facts['collapsed_point_rings']
    properties = {key: value for key, value in properties.items() if value is not None}
    identity_payload = {'properties': properties, 'geometry': geometry}
    fingerprint = hashlib.sha256(json.dumps(
        identity_payload, sort_keys=True, separators=(',', ':'),
        ensure_ascii=False).encode('utf-8')).hexdigest()
    # Preserve the authoritative source row identity in the archive while
    # keeping the semantic duplicate audit independent of ArcGIS OBJECTID.
    properties['source_oid'] = source_oid
    return (source_oid, serial, fingerprint, properties, geometry,
            geometry_facts, source_geometry_sha256)


def _stream_claims(snapshot, paths, precision_path=None):
    if snapshot.get('state') != 'AK' or snapshot.get('system_id') != 'alaska_state_claims':
        raise ValueError('claims staging is not the Alaska state-claim system')
    if snapshot.get('retrieved') != SNAPSHOT_DATE:
        raise ValueError(
            f'claims retrieved date must be the reviewed snapshot {SNAPSHOT_DATE}')
    if snapshot.get('source') != CLAIMS_SOURCE:
        raise ValueError('claims staging source endpoint is not canonical')
    if snapshot.get('snapshot_contract') != SNAPSHOT_CONTRACT:
        raise ValueError('claims staging lacks the reviewed snapshot contract')
    source_inventory = snapshot.get('source_inventory')
    if not isinstance(source_inventory, dict) or \
            set(source_inventory) != set(CLAIM_STATUSES):
        raise ValueError('claims source inventory must have exactly three status layers')
    for status in CLAIM_STATUSES:
        _reviewed_source_inventory(
            source_inventory[status], EXPECTED_CLAIM_SOURCE_INVENTORY[status],
            f'claims {status}')
    layers = snapshot.get('layers')
    if not isinstance(layers, dict) or set(layers) != set(CLAIM_STATUSES):
        raise ValueError('claims staging must have exactly active/pending/closed layers')
    for status in CLAIM_STATUSES:
        rows = layers[status]
        expected = EXPECTED_CLAIM_COUNTS[status]
        if not isinstance(rows, list) or len(rows) != expected:
            found = len(rows) if isinstance(rows, list) else 'non-list'
            raise ValueError(
                f'claims {status} count is {found}; reviewed snapshot requires {expected}')
    if set(paths) != set(CLAIM_STATUSES) or \
            len(set(map(os.path.realpath, paths.values()))) != len(CLAIM_STATUSES):
        raise ValueError('claim output paths must be distinct active/pending/closed files')
    if precision_path is not None:
        if not isinstance(precision_path, dict) or \
                set(precision_path) != set(PRECISION_LAYERS):
            raise ValueError(
                'claim precision paths must have active and closed outputs')
        all_output_paths = [*paths.values(), *precision_path.values()]
        if len(set(map(os.path.realpath, all_output_paths))) != \
                len(all_output_paths):
            raise ValueError('claim base/precision output paths must be distinct')

    serial_counts = collections.Counter()
    record_counts = collections.Counter()
    normalized = {status: [] for status in CLAIM_STATUSES}
    geometry_facts = collections.Counter()
    serial_status = {}
    for status in CLAIM_STATUSES:
        previous_source_oid = 0
        for index, row in enumerate(layers[status]):
            (source_oid, serial, fingerprint, properties, geometry, facts,
             source_geometry_sha256) = \
                _claim_record(row, status, index)
            if source_oid <= previous_source_oid:
                raise ValueError(
                    f'claims {status} source OBJECTIDs are not strictly ascending')
            previous_source_oid = source_oid
            previous_status = serial_status.setdefault(serial, status)
            if previous_status != status:
                raise ValueError(
                    f'claim serial {serial} occurs in both {previous_status} and {status}')
            serial_counts[(status, serial)] += 1
            record_counts[(status, fingerprint)] += 1
            geometry_facts.update(facts)
            normalized[status].append(
                (source_oid, serial, fingerprint, properties, geometry,
                 source_geometry_sha256))

    seen_occurrences = collections.Counter()
    feature_ids = set()
    feature_ids_by_status = {status: set() for status in CLAIM_STATUSES}
    source_oids_by_status = {status: set() for status in CLAIM_STATUSES}
    precision_feature_ids = {status: set() for status in PRECISION_LAYERS}
    precision_source_oids = {status: set() for status in PRECISION_LAYERS}
    precision_geometry_sha256 = {status: {} for status in PRECISION_LAYERS}
    emitted = collections.Counter()
    base_emitted = collections.Counter()
    future_labor_dates = 0
    outputs = {status: open(paths[status], 'w', encoding='utf-8')
               for status in CLAIM_STATUSES}
    if precision_path is None:
        precision_outputs = None
    else:
        precision_outputs = {
            status: open(precision_path[status], 'w', encoding='utf-8')
            for status in PRECISION_LAYERS
        }
    try:
        for status in CLAIM_STATUSES:
            for (source_oid, serial, fingerprint, properties, geometry,
                 source_geometry_sha256) in \
                    normalized[status]:
                occurrence_key = (status, fingerprint)
                seen_occurrences[occurrence_key] += 1
                occurrence = seen_occurrences[occurrence_key]
                identity = f'{status}\x1f{source_oid}\x1f{fingerprint}\x1f{occurrence}'
                feature_id = _feature_id('ak-state-claim', identity)
                if feature_id in feature_ids:
                    raise ValueError(
                        f'deterministic claim feature-ID collision at {serial}')
                feature_ids.add(feature_id)
                source_oids_by_status[status].add(source_oid)
                if properties.get('labor', '0000') > SNAPSHOT_DATE:
                    future_labor_dates += 1
                props = dict(properties)
                props['part'] = fingerprint[:12] + (
                    f'-{occurrence}' if record_counts[occurrence_key] > 1 else '')
                if serial_counts[(status, serial)] > 1:
                    props['parts'] = serial_counts[(status, serial)]
                props['fid'] = feature_id
                feature = {
                    'type': 'Feature', 'id': feature_id,
                    'properties': props, 'geometry': geometry,
                }
                is_precision = source_oid in \
                    EXPECTED_PRECISION_SOURCE_GEOMETRY_SHA256.get(status, {})
                if is_precision:
                    if precision_outputs is None:
                        # Source audits may intentionally omit a precision file;
                        # all rows are then emitted to their ordinary layer.
                        destination = outputs[status]
                        feature_ids_by_status[status].add(feature_id)
                        base_emitted[status] += 1
                    else:
                        destination = precision_outputs[status]
                        precision_feature_ids[status].add(feature_id)
                        precision_source_oids[status].add(source_oid)
                        precision_geometry_sha256[status][source_oid] = \
                            source_geometry_sha256
                else:
                    destination = outputs[status]
                    feature_ids_by_status[status].add(feature_id)
                    base_emitted[status] += 1
                json.dump(feature, destination, separators=(',', ':'),
                          ensure_ascii=False, allow_nan=False)
                destination.write('\n')
                emitted[status] += 1
    finally:
        for output in outputs.values():
            output.close()
        if precision_outputs is not None:
            for output in precision_outputs.values():
                output.close()
    if emitted != collections.Counter(EXPECTED_CLAIM_COUNTS):
        raise RuntimeError(f'claim emission mismatch: {dict(emitted)}')
    if precision_path is not None:
        for status in PRECISION_LAYERS:
            expected_precision_oids = set(
                EXPECTED_PRECISION_SOURCE_GEOMETRY_SHA256[status])
            if precision_source_oids[status] != expected_precision_oids:
                raise RuntimeError(
                    f'precision {status} source OBJECTID routing mismatch: '
                    f'missing={sorted(expected_precision_oids - precision_source_oids[status])}, '
                    f'extra={sorted(precision_source_oids[status] - expected_precision_oids)}')
            if precision_geometry_sha256[status] != \
                    EXPECTED_PRECISION_SOURCE_GEOMETRY_SHA256[status]:
                raise RuntimeError(
                    f'precision {status} source geometry hash inventory changed')
    all_base_ids = set().union(*feature_ids_by_status.values())
    all_precision_ids = set().union(*precision_feature_ids.values())
    if all_base_ids & all_precision_ids or \
            all_base_ids | all_precision_ids != feature_ids:
        raise RuntimeError('base/precision feature-ID partition is not disjoint and exact')
    return {
        'n': sum(emitted.values()),
        'by_status': {status: emitted[status] for status in CLAIM_STATUSES},
        'base_n': sum(base_emitted.values()),
        'base_by_status': {
            status: base_emitted[status] for status in CLAIM_STATUSES},
        'precision_n': sum(map(len, precision_feature_ids.values())),
        'precision_by_status': {
            status: len(precision_feature_ids[status])
            for status in PRECISION_LAYERS},
        'precision_source_objectids': {
            status: sorted(precision_source_oids.get(status, ()))
            for status in PRECISION_LAYERS},
        'precision_source_geometry_sha256': {
            status: {
                str(key): precision_geometry_sha256[status][key]
                for key in sorted(precision_geometry_sha256[status])
            } for status in PRECISION_LAYERS
        },
        'unique_serials': len(serial_status),
        'repeated_serial_rows': sum(count - 1 for count in serial_counts.values()),
        'exact_duplicate_rows': sum(count - 1 for count in record_counts.values()),
        'geometry': dict(sorted(geometry_facts.items())),
        'future_labor_dates': future_labor_dates,
        '_feature_ids': {
            status: sorted(feature_ids_by_status[status])
            for status in CLAIM_STATUSES
        },
        '_precision_feature_ids': {
            PRECISION_LAYERS[status]: sorted(precision_feature_ids[status])
            for status in PRECISION_LAYERS
        },
        '_source_oids': {
            status: sorted(source_oids_by_status[status])
            for status in CLAIM_STATUSES
        },
        '_base_source_oids': {
            status: sorted(
                source_oids_by_status[status] -
                precision_source_oids.get(status, set()))
            for status in CLAIM_STATUSES
        },
        '_precision_source_oids': {
            status: sorted(precision_source_oids[status])
            for status in PRECISION_LAYERS},
    }


def _ardf_record(row, index, truncations):
    label = f'ardf.features[{index}]'
    if not isinstance(row, dict) or not isinstance(row.get('properties'), dict):
        raise ValueError(f'{label} must contain properties and geometry')
    properties = row['properties']
    if not ARDF_REQUIRED <= set(properties):
        raise ValueError(
            f'{label}.properties is missing {sorted(ARDF_REQUIRED - set(properties))}')
    oid = properties.get('OBJECTID')
    if isinstance(oid, bool) or not isinstance(oid, int) or oid <= 0:
        raise ValueError(f'{label}.OBJECTID must be a positive integer')
    ardf_number = _text(
        properties.get('ARDF_no'), f'{label}.ARDF_no', required=True,
        maximum=20)
    if not ARDF_NUMBER_RE.fullmatch(ardf_number):
        raise ValueError(f'{label}.ARDF_no is not a canonical ARDF identifier')
    geometry = row.get('geometry')
    if not isinstance(geometry, dict) or set(geometry) < {'x', 'y'}:
        raise ValueError(f'{label}.geometry must be an Esri point')
    longitude = _finite(
        geometry['x'], f'{label}.geometry.x', minimum=-180, maximum=180)
    latitude = _finite(
        geometry['y'], f'{label}.geometry.y', minimum=50, maximum=73)
    attr_longitude = _finite(
        properties.get('Longitude'), f'{label}.Longitude', minimum=-180,
        maximum=180)
    attr_latitude = _finite(
        properties.get('Latitude'), f'{label}.Latitude', minimum=50, maximum=73)
    if abs(longitude - attr_longitude) > 0.01 or abs(latitude - attr_latitude) > 0.01:
        raise ValueError(f'{label} attribute coordinates disagree with geometry')
    source_state = properties.get('State')
    if source_state not in ('AK', ''):
        raise ValueError(f'{label}.State is not AK or the known source blank')
    main_commodities = _text(
        properties.get('Commodities_main'), f'{label}.Commodities_main',
        maximum=120)
    tokens = set(re.findall(r'[A-Z][A-Z0-9]*', (main_commodities or '').upper()))
    if 'AU' in tokens:
        commodity_group = 0
    elif tokens & {
            'AG', 'CU', 'PB', 'ZN', 'W', 'WO3', 'SB', 'HG', 'FE', 'CR',
            'CO', 'NI', 'MO', 'SN', 'MN', 'TI', 'V', 'BI', 'CD', 'AS',
            'SE', 'PGE', 'PT', 'PD', 'IR', 'OS', 'RH', 'RU'}:
        commodity_group = 1
    elif tokens & {'U', 'TH', 'REE', 'RE', 'LI', 'BE', 'NB', 'TA'}:
        commodity_group = 2
    elif tokens & {'COAL', 'LIGNITE', 'PEAT', 'GEOTHERMAL'}:
        commodity_group = 4
    elif tokens & {
            'STONE', 'GRAVEL', 'SAND', 'LIMESTONE', 'MARBLE', 'MICA',
            'GARNET', 'BARITE', 'FLUORITE', 'GRAPHITE', 'GYPSUM',
            'ASBESTOS', 'PHOSPHATE', 'SULFUR', 'DIATOMITE', 'CLAY',
            'QUARTZ', 'SILICA'}:
        commodity_group = 3
    else:
        commodity_group = 5
    site_status = _text(
        properties.get('Site_status'), f'{label}.Site_status', maximum=80)
    production = _text(
        properties.get('Production'), f'{label}.Production', maximum=80)
    existing = int(bool(re.match(r'(?i)^active\b', site_status or '') or
                        re.match(r'(?i)^yes\b', production or '')))
    site_type = _text(
        properties.get('Site_type'), f'{label}.Site_type', maximum=80)
    commodities_status = (SOURCE_VALUE_REPORTED if main_commodities is not None
                          else SOURCE_VALUE_BLANK)
    site_type_status = (SOURCE_VALUE_REPORTED if site_type is not None
                        else SOURCE_VALUE_BLANK)
    district = _text(
        properties.get('District'), f'{label}.District', maximum=100)
    district_status = (SOURCE_VALUE_REPORTED if district is not None
                       else SOURCE_VALUE_BLANK)
    compact = {
        'st': 'AK',
        'id': ardf_number,
        'nm': _text(properties.get('Site'), f'{label}.Site', required=True, maximum=400),
        'g': main_commodities or SOURCE_VALUE_MISSING,
        'g_status': commodities_status,
        'group': commodity_group,
        'ex': existing,
        'other': _text(
            properties.get('Commodities_other'), f'{label}.Commodities_other',
            maximum=120),
        'ore': _text(properties.get('Ore_minerals'), f'{label}.Ore_minerals', maximum=500),
        'gangue': _text(
            properties.get('Gangue_minerals'), f'{label}.Gangue_minerals', maximum=300),
        'typ': site_type or SOURCE_VALUE_MISSING,
        'typ_status': site_type_status,
        'status': site_status,
        'production': production,
        'model': _excerpt(properties.get('Deposit_model'), f'{label}.model', 500,
                          truncations),
        'district': district or SOURCE_VALUE_MISSING,
        'district_status': district_status,
        'quad': _text(properties.get('Quadrangle'), f'{label}.Quadrangle', maximum=100),
        'mrds': _text(properties.get('MRDS_no'), f'{label}.MRDS_no', maximum=100),
        'age': _excerpt(properties.get('Age'), f'{label}.age', 300, truncations),
        'host': _text(properties.get('Host_rock'), f'{label}.Host_rock', maximum=500),
        'host_age': _text(
            properties.get('Host_rock_age'), f'{label}.Host_rock_age', maximum=200),
        'igneous': _text(
            properties.get('Assoc_ign_rock'), f'{label}.Assoc_ign_rock', maximum=200),
        'geo': _excerpt(
            properties.get('Geologic_description'), f'{label}.geo', 700,
            truncations),
        'work': _excerpt(
            properties.get('Workings_exploration'), f'{label}.work', 500,
            truncations),
        'loc': _excerpt(properties.get('Location'), f'{label}.loc', 400,
                        truncations),
        'ref': _text(
            properties.get('Primary_reference'), f'{label}.Primary_reference',
            maximum=300),
    }
    compact = {key: value for key, value in compact.items() if value is not None}
    feature_id = _feature_id('ardf', ardf_number)
    compact['fid'] = feature_id
    source_blank_fields = frozenset(
        name for name, status in (
            ('commodities_main', commodities_status),
            ('site_type', site_type_status),
            ('district', district_status),
        ) if status == SOURCE_VALUE_BLANK)
    return oid, ardf_number, feature_id, {
        'type': 'Feature',
        'id': feature_id,
        'properties': compact,
        'geometry': {
            'type': 'Point',
            'coordinates': [round(longitude, 6), round(latitude, 6)],
        },
    }, int(source_state == ''), source_blank_fields


def _stream_ardf(snapshot, path):
    if snapshot.get('state') != 'AK' or snapshot.get('source_id') != 'ardf':
        raise ValueError('ARDF staging is not the Alaska ARDF source')
    if snapshot.get('retrieved') != SNAPSHOT_DATE:
        raise ValueError(f'ARDF retrieved date must be {SNAPSHOT_DATE}')
    if snapshot.get('source') != ARDF_SOURCE:
        raise ValueError('ARDF staging source endpoint is not canonical')
    if snapshot.get('snapshot_contract') != SNAPSHOT_CONTRACT:
        raise ValueError('ARDF staging lacks the reviewed snapshot contract')
    _reviewed_source_inventory(
        snapshot.get('source_inventory'), EXPECTED_ARDF_SOURCE_INVENTORY,
        'ARDF')
    if snapshot.get('n') != EXPECTED_ARDF_COUNT:
        raise ValueError(
            f'ARDF declared n={snapshot.get("n")}; expected {EXPECTED_ARDF_COUNT}')
    rows = snapshot.get('features')
    if not isinstance(rows, list) or len(rows) != EXPECTED_ARDF_COUNT:
        found = len(rows) if isinstance(rows, list) else 'non-list'
        raise ValueError(f'ARDF feature count is {found}; expected {EXPECTED_ARDF_COUNT}')
    oids = set()
    numbers = set()
    feature_ids = set()
    browser_properties_by_id = {}
    truncations = collections.Counter()
    source_state_blanks = 0
    source_blank_fields = collections.Counter()
    source_blank_site_type_oids = set()
    previous_oid = 0
    with open(path, 'w', encoding='utf-8') as output:
        for index, row in enumerate(rows):
            oid, number, feature_id, feature, blank, blank_fields = _ardf_record(
                row, index, truncations)
            if oid in oids:
                raise ValueError(f'ARDF has duplicate OBJECTID {oid}')
            if number in numbers:
                raise ValueError(f'ARDF has duplicate normalized ARDF_no {number}')
            if feature_id in feature_ids:
                raise ValueError(f'deterministic ARDF feature-ID collision at {number}')
            if oid <= previous_oid:
                raise ValueError('ARDF staging is not in strict ascending OBJECTID order')
            oids.add(oid)
            numbers.add(number)
            feature_ids.add(feature_id)
            browser_properties_by_id[feature_id] = {
                field: feature['properties'][field]
                for field in ARDF_BROWSER_FIELDS}
            previous_oid = oid
            source_state_blanks += blank
            source_blank_fields.update(blank_fields)
            if 'site_type' in blank_fields:
                source_blank_site_type_oids.add(oid)
            json.dump(feature, output, separators=(',', ':'), ensure_ascii=False,
                      allow_nan=False)
            output.write('\n')
    if source_blank_site_type_oids != EXPECTED_ARDF_BLANK_SITE_TYPE_OBJECTIDS:
        raise ValueError(
            'ARDF blank Site_type OBJECTID inventory changed; '
            f'missing={sorted(EXPECTED_ARDF_BLANK_SITE_TYPE_OBJECTIDS - source_blank_site_type_oids)}, '
            f'extra={sorted(source_blank_site_type_oids - EXPECTED_ARDF_BLANK_SITE_TYPE_OBJECTIDS)}')
    return {
        'n': len(rows),
        'unique_objectids': len(oids),
        'unique_ardf_numbers': len(numbers),
        'first_objectid': min(oids),
        'maximum_objectid': max(oids),
        'source_state_blanks': source_state_blanks,
        'source_blank_fields': dict(sorted(source_blank_fields.items())),
        'source_blank_site_type_objectids': sorted(source_blank_site_type_oids),
        'text_truncations': dict(sorted(truncations.items())),
        '_feature_ids': {'ardf': sorted(feature_ids)},
        '_browser_properties_by_id': browser_properties_by_id,
    }


def _description(dataset, counts, staging_sha256):
    return json.dumps({
        'schema': 'nwmm-alaska-pmtiles-v1',
        'dataset': dataset,
        'snapshot': SNAPSHOT_DATE,
        'counts': counts,
        'staging_sha256': staging_sha256,
    }, sort_keys=True, separators=(',', ':'))


def _decompress(data, compression):
    if compression == 1:
        return data
    if compression == 2:
        return gzip.decompress(data)
    raise ValueError(f'unsupported PMTiles internal compression {compression}')


def _validate_pmtiles(path, expected_layers, expected_description):
    """Validate v3 internals, exact layers/fields, and embedded count identity."""
    try:
        with open(path, 'rb') as archive:
            header = archive.read(127)
    except OSError as exc:
        raise ValueError(f'tippecanoe did not create {path}') from exc
    if len(header) != 127 or header[:8] != b'PMTiles\x03':
        raise ValueError(f'{path} is not a PMTiles v3 archive')
    size = os.path.getsize(path)
    values = struct.unpack_from('<11Q', header, 8)
    (root_offset, root_length, metadata_offset, metadata_length,
     leaf_offset, leaf_length, tile_offset, tile_length,
     addressed, entries, contents) = values
    ranges = [
        (root_offset, root_length, 'root directory'),
        (metadata_offset, metadata_length, 'metadata'),
        (tile_offset, tile_length, 'tile data'),
    ]
    if leaf_length:
        ranges.append((leaf_offset, leaf_length, 'leaf directory'))
    for offset, length, label in ranges:
        if offset < 127 or length <= 0 or offset + length > size:
            raise ValueError(f'invalid PMTiles {label} range')
    ordered = sorted(ranges)
    for left, right in zip(ordered, ordered[1:]):
        if left[0] + left[1] > right[0]:
            raise ValueError('PMTiles archive ranges overlap')
    if not (addressed > 0 and entries > 0 and contents > 0 and
            addressed >= entries >= contents):
        raise ValueError('PMTiles archive declares no usable tile entries')
    if header[99] != 1:
        raise ValueError(f'PMTiles tile type {header[99]} is not vector MVT')
    if header[100] > header[101] or header[101] > 24:
        raise ValueError('PMTiles zoom range is invalid')
    with open(path, 'rb') as archive:
        archive.seek(metadata_offset)
        metadata_bytes = archive.read(metadata_length)
    try:
        metadata = json.loads(_decompress(metadata_bytes, header[97]))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f'PMTiles metadata is invalid: {exc}') from exc
    layers = metadata.get('vector_layers')
    if not isinstance(layers, list):
        raise ValueError('PMTiles metadata has no vector_layers array')
    by_id = {layer.get('id'): layer for layer in layers if isinstance(layer, dict)}
    if set(by_id) != set(expected_layers):
        raise ValueError(
            f'PMTiles source layers are {sorted(by_id)}, expected {sorted(expected_layers)}')
    for layer, required_fields in expected_layers.items():
        fields = by_id[layer].get('fields')
        if not isinstance(fields, dict) or not set(required_fields) <= set(fields):
            raise ValueError(
                f'PMTiles layer {layer} is missing fields '
                f'{sorted(set(required_fields) - set(fields or {}))}')
        wrong_types = {
            field: fields.get(field)
            for field in set(required_fields) & NUMERIC_PM_FIELDS
            if fields.get(field) != 'Number'
        }
        if wrong_types:
            raise ValueError(
                f'PMTiles layer {layer} has nonnumeric fields {wrong_types}')
    try:
        embedded = json.loads(metadata.get('description', ''))
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError('PMTiles description does not contain count metadata') from exc
    if embedded != json.loads(expected_description):
        raise ValueError('PMTiles embedded snapshot/count metadata does not match input')
    return {
        'version': 3,
        'bytes': size,
        'source_layers': list(expected_layers),
        'tile_entries': entries,
        'tile_contents': contents,
        'embedded': embedded,
    }


def _pmtiles_json_metadata(path):
    """Read decompressed PMTiles metadata for path-leak/rebuild auditing."""
    try:
        with open(path, 'rb') as archive:
            header = archive.read(127)
            if len(header) != 127 or header[:8] != b'PMTiles\x03':
                raise ValueError(f'{path} is not a PMTiles v3 archive')
            metadata_offset, metadata_length = struct.unpack_from(
                '<2Q', header, 24)
            archive.seek(metadata_offset)
            payload = archive.read(metadata_length)
    except OSError as exc:
        raise ValueError(f'cannot read PMTiles metadata from {path}') from exc
    try:
        value = json.loads(_decompress(payload, header[97]))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f'PMTiles metadata is invalid: {exc}') from exc
    if not isinstance(value, dict):
        raise ValueError('PMTiles metadata must be an object')
    return value


def _path_independent_metadata(path, expected_description, attribution):
    """Pin deterministic metadata while forbidding private staging leakage."""
    metadata = _pmtiles_json_metadata(path)
    options = metadata.get('generator_options')
    generator = metadata.get('generator')
    expected_name = os.path.basename(path)
    if (metadata.get('name') != expected_name or
            metadata.get('description') != expected_description or
            metadata.get('attribution') != attribution or
            not isinstance(generator, str) or
            not re.fullmatch(r'tippecanoe v?\d+(?:\.\d+){1,2}', generator) or
            not isinstance(options, str) or '/' in options or '\\' in options):
        raise ValueError(
            f'{expected_name} PMTiles identity/options are not path-free')
    serialized = json.dumps(
        metadata, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
        allow_nan=False)
    forbidden = (os.path.realpath(os.path.dirname(path)),
                 os.sep + 'nwmm-alaska-pmtiles-')
    leaks = [marker for marker in forbidden if marker and marker in serialized]
    if leaks:
        raise ValueError(
            f'{expected_name} PMTiles metadata leaks private staging identity')
    return {
        'status': 'complete_path_free_reproducible_metadata',
        'name': expected_name,
        'metadata_sha256': _canonical_sha256(metadata),
        'generator_options_sha256': _canonical_sha256(options),
    }


def _run_tippecanoe(output, layer_paths, description, attribution, maximum_zoom):
    working_directory = os.path.realpath(os.path.dirname(output))
    if any(os.path.realpath(os.path.dirname(path)) != working_directory
           for path in layer_paths.values()):
        raise ValueError('tippecanoe inputs and output must share one staging directory')
    command = [
        'tippecanoe', '--force', '--output', os.path.basename(output),
        '--minimum-zoom=0', f'--maximum-zoom={maximum_zoom}',
        f'--base-zoom={maximum_zoom}', '--no-feature-limit',
        '--no-tile-size-limit',
        '--simplify-only-low-zooms',
        '--no-tiny-polygon-reduction-at-maximum-zoom',
        '--use-attribute-for-id=fid', '--exclude=fid',
        '--preserve-input-order', '--quiet', f'--description={description}',
        f'--attribution={attribution}',
    ]
    for layer, path in layer_paths.items():
        command.extend(('-L', f'{layer}:{os.path.basename(path)}'))
    environment = os.environ.copy()
    # Fix the worker count so logical output does not depend on machine size.
    # (PMTiles gzip streams can still differ byte-for-byte between runs, so the
    # builder's determinism contract is stable IDs/counts/schema rather than a
    # predeclared archive checksum.)
    environment['TIPPECANOE_MAX_THREADS'] = '1'
    subprocess.run(
        command, check=True, env=environment, cwd=working_directory)


def _feature_id_inventory(path, expected_layers, expected_ids,
                          feature_properties=None):
    """Full-scan MVT IDs and require exact source-record preservation."""
    metadata = _strict_pmtiles_header(
        path, list(expected_layers), feature_properties or expected_layers,
        verify_feature_properties=True, collect_feature_ids=True)
    result = {}
    for layer in expected_layers:
        expected = sorted(expected_ids[layer])
        observed = metadata.get('maxzoom_feature_ids', {}).get(layer, [])
        instances = metadata.get('maxzoom_feature_instances', {}).get(layer, 0)
        if observed != expected or instances < len(expected):
            expected_set, observed_set = set(expected), set(observed)
            raise ValueError(
                f'Alaska {layer} PMTiles feature-ID reconciliation failed; '
                f'missing={sorted(expected_set - observed_set)[:25]}, '
                f'extra={sorted(observed_set - expected_set)[:25]}, '
                f'instances={instances}, source_records={len(expected)}')
        result[layer] = {
            'status': 'complete_at_retrieval',
            'source_records': len(expected),
            'maxzoom_feature_instances': instances,
            'maxzoom_unique_tiled_ids': len(observed),
            'ids_sha256': hashlib.sha256(json.dumps(
                expected, separators=(',', ':'),
                allow_nan=False).encode('utf-8')).hexdigest(),
        }
    return result


def _pmtiles_maxzoom_features(path, expected_layer):
    """Yield every decoded feature from every unique maxzoom tile content."""
    with open(path, 'rb') as archive:
        header = archive.read(127)
        if len(header) != 127 or header[:8] != b'PMTiles\x03':
            raise ValueError(f'{path} is not a PMTiles v3 archive')
        (root_offset, root_length, _metadata_offset, _metadata_length,
         leaf_offset, leaf_length, tile_offset, tile_length,
         _addressed, _entries, _contents) = struct.unpack_from(
             '<11Q', header, 8)
        internal_compression = header[97]
        tile_compression = header[98]
        maximum_zoom = header[101]
        archive.seek(root_offset)
        directory = _directory_entries(_decompress_pmtiles(
            archive.read(root_length), internal_compression))
        tile_entries = []
        for tile_id, run_length, length, offset in directory:
            if run_length:
                tile_entries.append((tile_id, run_length, length, offset))
                continue
            if not leaf_length or offset + length > leaf_length:
                raise ValueError('PMTiles root points outside its leaf directory')
            archive.seek(leaf_offset + offset)
            leaf = _directory_entries(_decompress_pmtiles(
                archive.read(length), internal_compression))
            if any(item[1] == 0 for item in leaf):
                raise ValueError('PMTiles leaf directory contains a nested pointer')
            tile_entries.extend(leaf)
        maxzoom_first = (4 ** maximum_zoom - 1) // 3
        maxzoom_after = (4 ** (maximum_zoom + 1) - 1) // 3
        content_ranges = sorted({
            (offset, length)
            for tile_id, run_length, length, offset in tile_entries
            if tile_id < maxzoom_after and tile_id + run_length > maxzoom_first
        })
        for offset, length in content_ranges:
            if offset + length > tile_length:
                raise ValueError('PMTiles directory points outside tile data')
            archive.seek(tile_offset + offset)
            payload = archive.read(length)
            if len(payload) != length:
                raise ValueError('PMTiles tile payload is truncated')
            for layer in _mvt_layers(
                    _decompress_pmtiles(payload, tile_compression),
                    semantic=True):
                if layer['name'] == expected_layer:
                    yield from layer['features']


def _validate_ardf_status_property(properties, value_field, status_field,
                                   label):
    value = properties.get(value_field)
    status = properties.get(status_field)
    if status not in (SOURCE_VALUE_REPORTED, SOURCE_VALUE_BLANK):
        raise ValueError(f'{label}.{status_field} is not a source-value status')
    if status == SOURCE_VALUE_BLANK:
        if value != SOURCE_VALUE_MISSING:
            raise ValueError(
                f'{label}.{value_field} source_blank lacks the explicit sentinel')
    elif (not isinstance(value, str) or not value.strip() or
          value == SOURCE_VALUE_MISSING):
        raise ValueError(
            f'{label}.{value_field} reported value is empty or the missing sentinel')


def _ardf_semantic_inventory(path, expected_properties_by_id):
    """Require exact maxzoom browser values and source-blank semantics."""
    if (not isinstance(expected_properties_by_id, dict) or
            any(not isinstance(key, int) or key <= 0 or
                not isinstance(value, dict) or
                set(value) != set(ARDF_BROWSER_FIELDS)
                for key, value in expected_properties_by_id.items())):
        raise ValueError('ARDF expected browser-property inventory is invalid')
    seen = set()
    instances = 0
    for feature in _pmtiles_maxzoom_features(path, 'ardf'):
        instances += 1
        feature_id = feature.get('id')
        properties = feature.get('properties')
        if not isinstance(feature_id, int) or isinstance(feature_id, bool):
            raise ValueError('ARDF maxzoom feature has no numeric top-level ID')
        if not isinstance(properties, dict):
            raise ValueError(f'ARDF maxzoom feature {feature_id} lacks properties')
        missing = set(ARDF_BROWSER_FIELDS) - set(properties)
        if missing:
            raise ValueError(
                f'ARDF maxzoom feature {feature_id} lacks {sorted(missing)}')
        for value_field, status_field in (
                ('g', 'g_status'), ('typ', 'typ_status'),
                ('district', 'district_status')):
            _validate_ardf_status_property(
                properties, value_field, status_field,
                f'ARDF maxzoom feature {feature_id}')
        expected = expected_properties_by_id.get(feature_id)
        if expected is None:
            raise ValueError(f'ARDF maxzoom feature ID {feature_id} is unexpected')
        observed = {field: properties[field] for field in ARDF_BROWSER_FIELDS}
        if observed != expected:
            changed = sorted(
                field for field in ARDF_BROWSER_FIELDS
                if observed[field] != expected[field])
            raise ValueError(
                f'ARDF maxzoom feature {feature_id} changed browser properties '
                f'{changed}')
        seen.add(feature_id)
    expected_ids = set(expected_properties_by_id)
    if seen != expected_ids:
        raise ValueError(
            'ARDF maxzoom semantic inventory is incomplete; '
            f'missing={sorted(expected_ids - seen)[:25]}, '
            f'extra={sorted(seen - expected_ids)[:25]}')
    return {
        'status': 'complete_exact_browser_properties',
        'unique_feature_ids': len(seen),
        'maxzoom_feature_instances': instances,
        'browser_fields': list(ARDF_BROWSER_FIELDS),
    }


def _combined_claim_id_inventory(claim_stats, base_inventory,
    precision_inventory):
    result = {}
    for status in CLAIM_STATUSES:
        base = base_inventory[status]
        base_ids = set(claim_stats['_feature_ids'][status])
        layer = PRECISION_LAYERS.get(status)
        precision = (precision_inventory[layer] if layer else {
            'source_records': 0,
            'maxzoom_feature_instances': 0,
            'maxzoom_unique_tiled_ids': 0,
        })
        precision_ids = (set(claim_stats['_precision_feature_ids'][layer])
                         if layer else set())
        if base_ids & precision_ids:
            raise ValueError(f'Alaska {status} base/precision feature IDs overlap')
        expected = sorted(base_ids | precision_ids)
        if len(expected) != EXPECTED_CLAIM_COUNTS[status]:
            raise ValueError(
                f'Alaska {status} base/precision feature-ID union is incomplete')
        precision_records = precision['source_records']
        precision_instances = precision['maxzoom_feature_instances']
        precision_unique = precision['maxzoom_unique_tiled_ids']
        if (base['source_records'] + precision_records != len(expected) or
                base['maxzoom_unique_tiled_ids'] + precision_unique != len(expected)):
            raise ValueError(
                f'Alaska {status} base/precision archive inventories do not '
                'reconcile to the source')
        result[status] = {
            'status': 'complete_at_retrieval',
            'source_records': len(expected),
            'maxzoom_feature_instances': (
                base['maxzoom_feature_instances'] + precision_instances),
            'maxzoom_unique_tiled_ids': len(expected),
            'ids_sha256': _canonical_sha256(expected),
            'base_records': base['source_records'],
            'precision_records': precision_records,
            'disjoint_union_complete': True,
        }
    return result


def _source_objectid_delivery_inventory(claim_stats):
    result = {}
    for status in CLAIM_STATUSES:
        source = set(claim_stats['_source_oids'][status])
        base = set(claim_stats['_base_source_oids'][status])
        overflow = set(claim_stats['_precision_source_oids'].get(status, ()))
        if base & overflow or base | overflow != source:
            raise ValueError(
                f'Alaska {status} source OBJECTID delivery partition is not '
                'disjoint and exact')
        if len(source) != EXPECTED_CLAIM_COUNTS[status]:
            raise ValueError(
                f'Alaska {status} source OBJECTID inventory count changed')
        result[status] = {
            'source': _integer_id_inventory(source),
            'base': _integer_id_inventory(base),
            'precision': _integer_id_inventory(overflow),
            'disjoint_union_complete': True,
        }
    return result


def _prepare_install(source, destination, expected_layers, expected_description):
    directory = os.path.dirname(destination)
    os.makedirs(directory, exist_ok=True)
    handle, pending = tempfile.mkstemp(
        prefix=f'.{os.path.basename(destination)}-', suffix='.tmp', dir=directory)
    try:
        os.fchmod(handle, 0o644)
        with os.fdopen(handle, 'wb') as target, open(source, 'rb') as archive:
            shutil.copyfileobj(archive, target, length=1024 * 1024)
            target.flush()
            os.fsync(target.fileno())
        metadata = _validate_pmtiles(
            pending, expected_layers, expected_description)
        return pending, metadata, _sha256(pending)
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


def _reserve_backup(path, label):
    handle, backup = tempfile.mkstemp(
        prefix=f'.{label}-rollback-', dir=os.path.dirname(path))
    os.close(handle)
    os.unlink(backup)
    return backup


def _publish_bundle(prepared, manifest_entries=None):
    """Replace all Alaska archives and optional manifest as one transaction."""
    manifest_backup = None
    archive_backups = []
    if manifest_entries is not None:
        handle, manifest_backup = tempfile.mkstemp(
            prefix='.manifest-alaska-rollback-', dir=os.path.dirname(MANIFEST))
        os.close(handle)
        shutil.copy2(MANIFEST, manifest_backup)
    try:
        for index, (pending, destination) in enumerate(prepared):
            backup = None
            if os.path.exists(destination):
                backup = _reserve_backup(destination, f'alaska-{index}')
                os.replace(destination, backup)
            archive_backups.append((destination, backup))
            os.replace(pending, destination)
        if manifest_entries is not None:
            _stamp_manifest(manifest_entries)
    except BaseException:
        rollback_errors = []
        for destination, backup in reversed(archive_backups):
            try:
                if backup is None:
                    if os.path.exists(destination):
                        os.unlink(destination)
                elif os.path.exists(backup):
                    os.replace(backup, destination)
            except BaseException as exc:  # pragma: no cover - catastrophic FS failure
                rollback_errors.append(f'{destination}: {exc}')
        if manifest_backup is not None:
            try:
                if os.path.exists(manifest_backup):
                    os.replace(manifest_backup, MANIFEST)
            except BaseException as exc:  # pragma: no cover - catastrophic FS failure
                rollback_errors.append(f'{MANIFEST}: {exc}')
        if rollback_errors:
            raise RuntimeError(
                'Alaska publication failed and rollback was incomplete: ' +
                '; '.join(rollback_errors))
        raise
    else:
        for _, backup in archive_backups:
            if backup is not None:
                os.unlink(backup)
        if manifest_backup is not None:
            os.unlink(manifest_backup)
    finally:
        for pending, _ in prepared:
            try:
                os.unlink(pending)
            except FileNotFoundError:
                pass


def _publish_after_grace(prepared, entries, update_manifest):
    """Keep prepared files recoverable/clean even during the grace window."""
    try:
        if update_manifest:
            print(
                'Alaska private QA is complete; atomic three-archive plus '
                'manifest publication begins in '
                f'{PUBLICATION_GRACE_SECONDS} seconds', flush=True)
            time.sleep(PUBLICATION_GRACE_SECONDS)
        else:
            print(
                'Alaska private artifacts validated; installing into the '
                'explicit private output directory', flush=True)
        _publish_bundle(prepared, entries if update_manifest else None)
    except BaseException:
        # _publish_bundle already cleans these after it starts. This second
        # idempotent pass covers interruption during the pre-publication grace.
        for pending, _ in prepared:
            try:
                os.unlink(pending)
            except FileNotFoundError:
                pass
        raise


def _stamp_manifest(entries):
    """Merge fresh Alaska entries into the latest manifest atomically."""
    try:
        with open(MANIFEST, encoding='utf-8') as source:
            manifest = json.load(source)
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f'cannot read current manifest for Alaska merge: {exc}') from exc
    baselines = manifest.get('national_baselines')
    if not isinstance(baselines, dict):
        raise RuntimeError('current manifest national_baselines must be an object')
    # Never use this state-only archive as evidence that Alaska's federal half
    # exists.  Alaska remains unreleased until the registry gate independently
    # sees both systems.
    baselines['alaska_state_claims'] = entries['alaska_state_claims']
    baselines['ardf'] = entries['ardf']
    directory = os.path.dirname(MANIFEST)
    current_mode = stat.S_IMODE(os.stat(MANIFEST).st_mode)
    handle, pending = tempfile.mkstemp(prefix='.manifest-alaska-', dir=directory)
    try:
        os.fchmod(handle, current_mode)
        with os.fdopen(handle, 'w', encoding='utf-8') as output:
            json.dump(manifest, output, separators=(',', ':'))
            output.flush()
            os.fsync(output.fileno())
        os.replace(pending, MANIFEST)
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


def build(claims_staging, ardf_staging, *, update_manifest=True,
          private_output_dir=None):
    if not shutil.which('tippecanoe'):
        raise RuntimeError('tippecanoe >=2.79 with PMTiles output is required')
    if private_output_dir is not None:
        if update_manifest:
            raise ValueError(
                'private output cannot be combined with a manifest update')
        output_directory = _private_output_directory(private_output_dir)
        claims_out = os.path.join(output_directory, 'ak-state.pmtiles')
        precision_out = os.path.join(
            output_directory, 'ak-state-precision.pmtiles')
        ardf_out = os.path.join(output_directory, 'ardf.pmtiles')
        output_mode = 'private_validation'
    else:
        claims_out = CLAIMS_OUT
        precision_out = CLAIMS_PRECISION_OUT
        ardf_out = ARDF_OUT
        output_mode = 'public_candidate'
    claims_path = _private_staging_path(claims_staging, 'claims')
    ardf_path = _private_staging_path(ardf_staging, 'ARDF')
    claims_sha256 = _sha256(claims_path)
    ardf_sha256 = _sha256(ardf_path)
    if claims_sha256 != EXPECTED_CLAIMS_STAGING_SHA256:
        raise ValueError(
            'claims staging SHA-256 does not match the reviewed 2026-08-13 snapshot')
    if ardf_sha256 != EXPECTED_ARDF_STAGING_SHA256:
        raise ValueError(
            'ARDF staging SHA-256 does not match the reviewed 2026-08-13 snapshot')
    claims_snapshot = _load_json(claims_path, 'claims')
    ardf_snapshot = _load_json(ardf_path, 'ARDF')

    claim_fields = ('st', 'system', 'source_oid', 'serial', 'status',
                    'source_status', 'acres', 'part', 'url', 'lon', 'lat')
    claims_layers = {status: claim_fields for status in CLAIM_STATUSES}
    precision_layers = {
        layer: claim_fields for layer in PRECISION_LAYERS.values()}
    ardf_layers = {'ardf': ARDF_BROWSER_FIELDS}
    with tempfile.TemporaryDirectory(prefix='nwmm-alaska-pmtiles-') as temporary:
        status_sequences = {
            status: os.path.join(temporary, f'{status}.geojsonseq')
            for status in CLAIM_STATUSES
        }
        precision_sequences = {
            status: os.path.join(temporary, f'{status}-precision.geojsonseq')
            for status in PRECISION_LAYERS}
        ardf_sequence = os.path.join(temporary, 'ardf.geojsonseq')
        claims_pending = os.path.join(temporary, 'ak-state.pmtiles')
        precision_pending = os.path.join(temporary, 'ak-state-precision.pmtiles')
        ardf_pending = os.path.join(temporary, 'ardf.pmtiles')
        claims_stats = _stream_claims(
            claims_snapshot, status_sequences, precision_sequences)
        ardf_stats = _stream_ardf(ardf_snapshot, ardf_sequence)
        claims_description = _description(
            'alaska_state_claims', claims_stats['base_by_status'], claims_sha256)
        precision_description = _description(
            'alaska_state_claims_precision',
            {
                PRECISION_LAYERS[status]:
                    claims_stats['precision_by_status'][status]
                for status in PRECISION_LAYERS
            }, claims_sha256)
        ardf_description = _description(
            'ardf', {'ardf': ardf_stats['n']}, ardf_sha256)
        _run_tippecanoe(
            claims_pending, status_sequences, claims_description,
            'Alaska Department of Natural Resources', CLAIMS_MAXZOOM)
        _run_tippecanoe(
            precision_pending, {
                PRECISION_LAYERS[status]: precision_sequences[status]
                for status in PRECISION_LAYERS
            },
            precision_description, 'Alaska Department of Natural Resources',
            CLAIMS_PRECISION_MAXZOOM)
        _run_tippecanoe(
            ardf_pending, {'ardf': ardf_sequence}, ardf_description,
            'U.S. Geological Survey Alaska Resource Data File', ARDF_MAXZOOM)
        _validate_pmtiles(
            claims_pending, claims_layers, claims_description)
        _validate_pmtiles(
            precision_pending, precision_layers, precision_description)
        _validate_pmtiles(ardf_pending, ardf_layers, ardf_description)
        path_independent_metadata = {
            'ak-state.pmtiles': _path_independent_metadata(
                claims_pending, claims_description,
                'Alaska Department of Natural Resources'),
            'ak-state-precision.pmtiles': _path_independent_metadata(
                precision_pending, precision_description,
                'Alaska Department of Natural Resources'),
            'ardf.pmtiles': _path_independent_metadata(
                ardf_pending, ardf_description,
                'U.S. Geological Survey Alaska Resource Data File'),
        }
        base_claims_id_inventory = _feature_id_inventory(
            claims_pending, claims_layers, claims_stats['_feature_ids'])
        precision_id_inventory = _feature_id_inventory(
            precision_pending, precision_layers,
            claims_stats['_precision_feature_ids'])
        claims_id_inventory = _combined_claim_id_inventory(
            claims_stats, base_claims_id_inventory, precision_id_inventory)
        source_objectid_inventory = _source_objectid_delivery_inventory(
            claims_stats)
        # Full-scan every browser-required property, not only the stable ID
        # anchors. Tippecanoe omits null attributes feature-by-feature, so a
        # metadata-only field check cannot prove semantic completeness.
        ardf_id_inventory = _feature_id_inventory(
            ardf_pending, ardf_layers, ardf_stats['_feature_ids'])
        ardf_semantic_inventory = _ardf_semantic_inventory(
            ardf_pending, ardf_stats['_browser_properties_by_id'])
        claim_install = None
        precision_install = None
        ardf_install = None
        try:
            claim_install = _prepare_install(
                claims_pending, claims_out, claims_layers, claims_description)
            precision_install = _prepare_install(
                precision_pending, precision_out, precision_layers,
                precision_description)
            ardf_install = _prepare_install(
                ardf_pending, ardf_out, ardf_layers, ardf_description)
        except BaseException:
            for prepared in (claim_install, precision_install, ardf_install):
                if prepared is not None:
                    try:
                        os.unlink(prepared[0])
                    except FileNotFoundError:
                        pass
            raise

    claims_manifest = {
        'file': 'data/tiles/claims/ak-state.pmtiles',
        'format': 'pmtiles',
        'source_layers': list(CLAIM_STATUSES),
        'n': claims_stats['n'],
        'by_status': claims_stats['by_status'],
        'system': 'alaska_state_claims',
        'jurisdiction': 'state',
        'retrieved': SNAPSHOT_DATE,
        'minzoom': 0,
        'maxzoom': CLAIMS_MAXZOOM,
        # The archive retains complete no-drop overview tiles for audit, but
        # the browser must not request their intentionally large payloads.
        'activation_zoom': 8,
        'source': CLAIMS_SOURCE,
        'staging_sha256': claims_sha256,
        'source_snapshot_contract': SNAPSHOT_CONTRACT,
        'source_snapshot_inventory': EXPECTED_CLAIM_SOURCE_INVENTORY,
        'source_quality': {
            'repeated_serial_rows': claims_stats['repeated_serial_rows'],
            'exact_duplicate_rows': claims_stats['exact_duplicate_rows'],
            'zero_area_rings': claims_stats['geometry'].get('zero_area_rings', 0),
            'zero_area_features': claims_stats['geometry'].get('zero_area_feature', 0),
            'collapsed_point_rings': claims_stats['geometry'].get(
                'collapsed_point_rings', 0),
            'counterclockwise_exteriors': claims_stats['geometry'].get(
                'counterclockwise_exteriors', 0),
            'future_labor_dates': claims_stats['future_labor_dates'],
        },
        'source_id_inventory': claims_id_inventory,
        'source_objectid_inventory': source_objectid_inventory,
        'base_delivery': {
            'n': claims_stats['base_n'],
            'by_status': claims_stats['base_by_status'],
            'source_id_inventory': base_claims_id_inventory,
        },
        'precision_overflow': {
            'file': 'data/tiles/claims/ak-state-precision.pmtiles',
            'format': 'pmtiles',
            'source_layers': list(PRECISION_LAYERS.values()),
            'n': claims_stats['precision_n'],
            'by_status': claims_stats['precision_by_status'],
            'source_objectids': claims_stats['precision_source_objectids'],
            'source_geometry_sha256':
                claims_stats['precision_source_geometry_sha256'],
            'source_id_inventory': precision_id_inventory,
            'minzoom': 0,
            'maxzoom': CLAIMS_PRECISION_MAXZOOM,
            'activation_zoom': CLAIMS_PRECISION_MAXZOOM,
            'bytes': precision_install[1]['bytes'],
            'sha256': precision_install[2],
            'note': ('Twenty-four unchanged official DNR source polygons '
                     'below the z13 MVT quantization grid; delivered '
                     'separately at z19 with no fabricated or expanded '
                     'geometry.'),
        },
        'bytes': claim_install[1]['bytes'],
        'sha256': claim_install[2],
        'note': ('Alaska state-law claims only; federal Alaska MLRS claims are a '
                 'separate required artifact and are not supplied by this archive.'),
    }
    ardf_manifest = {
        'file': 'data/tiles/national/ardf.pmtiles',
        'format': 'pmtiles',
        'source_layer': 'ardf',
        'n': ardf_stats['n'],
        'states': {'AK': ardf_stats['n']},
        'retrieved': SNAPSHOT_DATE,
        'minzoom': 0,
        'maxzoom': ARDF_MAXZOOM,
        'source': ARDF_SOURCE,
        'staging_sha256': ardf_sha256,
        'source_snapshot_contract': SNAPSHOT_CONTRACT,
        'source_snapshot_inventory': EXPECTED_ARDF_SOURCE_INVENTORY,
        'source_quality': {
            'source_state_blanks': ardf_stats['source_state_blanks'],
            'source_blank_fields': ardf_stats['source_blank_fields'],
            'source_blank_site_type_objectids':
                ardf_stats['source_blank_site_type_objectids'],
            'text_truncations': ardf_stats['text_truncations'],
        },
        'source_id_inventory': ardf_id_inventory,
        'bytes': ardf_install[1]['bytes'],
        'sha256': ardf_install[2],
        'note': 'Alaska Resource Data File occurrence backbone; richer than MRDS in Alaska.',
    }
    entries = {
        'alaska_state_claims': claims_manifest,
        'ardf': ardf_manifest,
    }
    _publish_after_grace([
        (claim_install[0], claims_out),
        (precision_install[0], precision_out),
        (ardf_install[0], ardf_out),
    ], entries, update_manifest)
    artifact_fingerprints = {
        'ak-state.pmtiles': claims_manifest['sha256'],
        'ak-state-precision.pmtiles': claims_manifest[
            'precision_overflow']['sha256'],
        'ardf.pmtiles': ardf_manifest['sha256'],
    }
    result = {
        'artifacts': entries,
        'artifact_fingerprints': artifact_fingerprints,
        'artifact_set_sha256': _canonical_sha256(artifact_fingerprints),
        'path_independent_pmtiles_metadata': path_independent_metadata,
        'path_independent_metadata_set_sha256': _canonical_sha256(
            path_independent_metadata),
        'verified': {
            'claims': {key: value for key, value in claims_stats.items()
                       if not key.startswith('_')},
            'ardf': {key: value for key, value in ardf_stats.items()
                     if not key.startswith('_')},
        },
        'manifest_updated': bool(update_manifest),
        'output_mode': output_mode,
    }
    result['verified']['ardf']['maxzoom_semantic_inventory'] = \
        ardf_semantic_inventory
    print(json.dumps(result, indent=2, sort_keys=True))
    return result


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--claims-staging', required=True,
                        help='private fetch_ak_claims JSON outside site/')
    parser.add_argument('--ardf-staging', required=True,
                        help='private fetch_ardf JSON outside site/')
    parser.add_argument('--no-manifest', action='store_true',
                        help='build only into --private-output-dir; do not publish')
    parser.add_argument('--private-output-dir',
                        help='private artifact directory outside site/ for QA')
    args = parser.parse_args(argv)
    if bool(args.no_manifest) != bool(args.private_output_dir):
        parser.error(
            '--no-manifest and --private-output-dir must be supplied together')
    try:
        build(args.claims_staging, args.ardf_staging,
              update_manifest=not args.no_manifest,
              private_output_dir=args.private_output_dir)
    except (ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
