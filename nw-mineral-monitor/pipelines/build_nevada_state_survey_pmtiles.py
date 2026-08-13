#!/usr/bin/env python3
"""Build honest Nevada state-survey PMTiles baselines without releasing Nevada.

Three browser-addressable archives are produced:

* USGS Data Series 249 geology and bedrock-fault layers (prepared with NBMG);
* NBMG's 2013 OneGeology 1:250,000 conversion layer; and
* NBMG Report 47 mining-district polygons.

The two ArcGIS services are snapshotted by object ID and every returned page is
reconciled to that snapshot.  DS 249 is downloaded as the original ZIP and is
accepted only at the reviewed byte count and SHA-256 below.  GeoJSON sequence
files exist only in a private build-input staging directory outside ``site/``.
The only public data products are PMTiles.

These are research baselines, not DONE-gate evidence.  Publishing them never
changes ``states/NV.yaml:release`` or any DONE-gate status.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

from common import TODAY
from state_registry import ALL_STATES

try:
    import fiona
    from fiona.transform import transform_geom
except ImportError:  # pragma: no cover - tested through the build preflight
    fiona = None
    transform_geom = None

try:
    import shapely
    from shapely import make_valid as shapely_make_valid
    from shapely.geometry import mapping as shapely_mapping
    from shapely.geometry import shape as shapely_shape
    from shapely.ops import unary_union as shapely_unary_union
    from shapely.prepared import prep as shapely_prepare
    from shapely.validation import explain_validity as shapely_explain_validity
except ImportError:  # pragma: no cover - tested through the build preflight
    shapely = None
    shapely_make_valid = None
    shapely_mapping = None
    shapely_shape = None
    shapely_unary_union = None
    shapely_prepare = None
    shapely_explain_validity = None


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
SITE = os.path.join(ROOT, 'site')
MANIFEST = os.path.join(SITE, 'data', 'manifest.json')
OUT_DIR = os.path.join(SITE, 'data', 'tiles', 'states', 'nv')
PRIVATE_STAGING_ROOT = os.path.join(ROOT, 'build-inputs', '.staging')
DS249_OUT = os.path.join(OUT_DIR, 'usgs-ds249.pmtiles')
ONEGEOLOGY_OUT = os.path.join(OUT_DIR, 'nbmg-onegeology-250k.pmtiles')
DISTRICTS_OUT = os.path.join(OUT_DIR, 'nbmg-report47-districts.pmtiles')
STATE_CLIPS = os.path.join(ROOT, 'infra', 'state_clips.json')

DS249_URL = 'https://pubs.usgs.gov/ds/2007/249/downloads/249.zip'
DS249_DOI = 'https://doi.org/10.3133/ds249'
DS249_BYTES = 183_472_749
DS249_SHA256 = '06da03ff35a08562baec56c6f889568dbbab562f11a18ca3226f2733bc44428e'
DS249_LAST_MODIFIED = '2007-12-21T04:13:45Z'
DS249_MEMBERS = {
    'geology': (
        'Database files/Geology/NevadaGeology.shp',
        'Database files/Geology/NevadaGeology.shx',
        'Database files/Geology/NevadaGeology.dbf',
        'Database files/Geology/NevadaGeology.prj',
        'Database files/Geology/NevadaGeology.shp.xml',
    ),
    'faults': (
        'Database files/Geology/StatewideFaults.shp',
        'Database files/Geology/StatewideFaults.shx',
        'Database files/Geology/StatewideFaults.dbf',
        'Database files/Geology/StatewideFaults.prj',
        'Database files/Geology/StatewideFaults.shp.xml',
    ),
}
# The reviewed DS 249 ZIP has two zero-area geology records with null shapes.
# They cannot be encoded in MVT and must remain explicit inventory exceptions.
DS249_NULL_GEOMETRY = {'geology': ['28814', '30918'], 'faults': []}
DS249_GEOMETRY_CONTRACT = {
    'geology': {
        'source_schema': 'Polygon',
        'by_type': {'Polygon': 38_623, 'MultiPolygon': 71},
        'by_type_object_ids_sha256': {
            'Polygon':
                'cac351f003ec2a0a9dbb4181dbb0d56574344122f725ac4494608032803691ed',
            'MultiPolygon':
                '9e1581fde710effa0cca63ddd69a6c490d50d006b1dd7e655c0696af5aed6ca0',
        },
    },
    'faults': {
        'source_schema': 'LineString',
        'by_type': {'LineString': 54_712, 'MultiLineString': 1},
        'by_type_object_ids_sha256': {
            'LineString':
                '19e09109a3f14f2d6151c72c055b29962286ec75d1dd6e20256bc06579e6df6a',
            'MultiLineString':
                'cff5cedb61f5477af93ad589c67ba571d77a726d1b690c225e6bfff35ee04173',
        },
    },
}
DS249_GEOLOGY_REPAIR_CONTRACT = {
    'count': 187,
    'object_ids_sha256':
        '8e29716492b8d72f241912b09e7aa7b80dfbc19b098a1db50368cb3c96fc3973',
    'validity_reason_counts': {'Ring Self-intersection': 187},
    'type_transition_counts': {
        'Polygon->Polygon->Polygon': 185,
        'MultiPolygon->MultiPolygon->MultiPolygon': 2,
    },
    'nonpolygon_parts_dropped_by_type': {},
    'shapely_version': '2.0.3', 'geos_version': '3.11.3',
    'max_absolute_area_delta': 1e-3,
    'max_relative_area_delta': 1e-12,
}
DS249_FAULT_REPAIR_CONTRACT = {
    'count': 0,
    'object_ids_sha256':
        '4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945',
    'validity_reason_counts': {}, 'type_transition_counts': {},
    'nonpolygon_parts_dropped_by_type': {},
    'shapely_version': '2.0.3', 'geos_version': '3.11.3',
    'max_absolute_area_delta': 0.0,
    'max_relative_area_delta': 0.0,
}
DS249_FAULT_ENCODING_EXCLUSION_CONTRACT = {
    # These valid two-vertex traces are 0.09375--1.908 m long in a 1:250,000
    # source and collapse under z12 MVT quantization. Do not invent a longer
    # fault merely to retain an icon-sized browser feature.
    'count': 15,
    'fids': [
        625, 937, 1451, 1486, 18632, 22509, 38619, 38881, 41702,
        41986, 47020, 47770, 48718, 49089, 49190,
    ],
    'fids_sha256':
        'a3b06f38a961b84840a195e0d3a88ce4c0cd39d1b04a775e06fb5fb0504998ec',
    'source_record_ids': [
        '624', '936', '1450', '1485', '18631', '22508', '38618',
        '38880', '41701', '41985', '47019', '47769', '48717',
        '49088', '49189',
    ],
    'source_record_ids_sha256':
        '90db304aed354e0c423483f5c5bd94ad91085078008a68da56e02554ac0ca6ff',
    'native_lengths_m': [
        1.0, 0.6875, 0.09375, 1.09375, 0.1875, 0.625, 0.09375,
        1.02364620963495, 1.25, 0.3125, 1.25, 1.0915155747858112,
        1.9080421903092184, 1.8027756377319946, 1.0476909133900132,
    ],
    'source_geometry_sha256': [
        '0f299d01f33f0f8231b6499d36cd665824540eb77e3cebae03ad2509baf1fd5b',
        '7b6755a4f603e722ce555af65eb8a719d1d8619d7403aa2dc036a068bdba4dee',
        '1587d7613064e6fc137b23e15bc769c325991e42ca800d16fa8169b867ab9125',
        '4ad71e226719ef6b17f210434b6cabc9ab74f59b52ae66ceb635cd9a90def953',
        'f936db01e3a4bd7a0ae6351cc33e597dc9c3bb34af9ede84581f318dd19247a9',
        'c156d70d4130afdb4b32765bd5ef50a94ea9bbe2246168d3848ed9e8b7ff78db',
        '8f7916dbd4d287822bc485e7002ed34eccf9bf4d3a0314ce2f6c3c19cf1c97da',
        '90508b8eadcee5a46e270672eb872d82fad30dbe3cce9620c0dc60e83f18205b',
        'ab5caca232e99bfb259944d4d01731848525bc577b77c1407b84a84920249b86',
        'dcb10ced85244ebf8eba5db9715910325e0cca00c6ee1194c18d56455dffd825',
        '46339472bf518a4dfe7590733037041d430ec4bddb4b124d681b52464c612bc0',
        '45690348a63b292ce03f6ad456404476112666e6ec6dfe5329c942ff74b07caf',
        'ec381caac81b46c03f6535d856c9639940c9290143a1c8adf8cb4e73832b17dc',
        'd8161151101c8dce4435b0dc3c20901ebaa467fa433b7dfa41773835e906523f',
        'c79bb41f353980d1c64639b3bdfdc57fc7bf704887be47d5afed7de6eeaa7180',
    ],
    'records_sha256':
        '84cf035b062a31aaca0b1a4608a268ee3a93e99d732f5a78e1811fb8edcbfa79',
    'source_geometry_hashes_sha256':
        'a977b282773e310e7b393b67cf41689f8eef9be2a06d05afa9bc326e14896201',
    'minimum_native_length_m': 0.09375,
    'maximum_native_length_m': 1.9080421903092184,
    'sum_native_length_m': 13.467420525851987,
    'maximum_accepted_native_length_m': 2.0,
    'reason': 'below_mvt_maxzoom_encoding_resolution',
    'tippecanoe_version': 'v2.79.0',
    'tippecanoe_maxzoom': 12,
    'tippecanoe_full_detail': 12,
}

NBMG_GEOLOGY = (
    'https://gisweb.unr.edu/nbmg/rest/services/Geology/'
    'NBMG_Geology/MapServer/23')
NBMG_GEOLOGY_CATALOG = (
    'https://www.nbmg.unr.edu/Maps%26Data/StatewideGeologicMaps.html')
# These are the OneGeology conversions of the same zero-area DS 249 source
# slivers. The live service returns typed Polygon objects with empty coordinate
# arrays; keep their current service IDs explicit in every source snapshot.
ONEGEOLOGY_EMPTY_GEOMETRY = [28833, 31033]
ONEGEOLOGY_REPAIR_CONTRACT = {
    'count': 189,
    'object_ids_sha256':
        'f5dc88e70fa77e0fc9c8d713a27787f4b2da49289dcb978d95b7b07cfc9553a3',
    'validity_reason_counts': {'Ring Self-intersection': 189},
    'type_transition_counts': {
        'Polygon->Polygon->Polygon': 187,
        'MultiPolygon->MultiPolygon->MultiPolygon': 2,
    },
    'nonpolygon_parts_dropped_by_type': {},
    'shapely_version': '2.0.3',
    'geos_version': '3.11.3',
    'max_absolute_area_delta': 1e-12,
    'max_relative_area_delta': 1e-12,
}
DISTRICT_REPAIR_CONTRACT = {
    'count': 0,
    'object_ids_sha256':
        '4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945',
    'validity_reason_counts': {}, 'type_transition_counts': {},
    'nonpolygon_parts_dropped_by_type': {},
    'shapely_version': '2.0.3', 'geos_version': '3.11.3',
    'max_absolute_area_delta': 1e-12,
    'max_relative_area_delta': 1e-12,
}
NBMG_DISTRICTS = (
    'https://gisweb.unr.edu/nbmg/rest/services/MineralsAndEnergy/'
    'mining_districts/MapServer/0')
NBMG_DISTRICTS_CATALOG = (
    'https://pubs.nbmg.unr.edu/CDP-Mining-districts-NV-2nd-ed-p/r047z.htm')

NV_BOUNDS = (-120.02, 34.97, -114.00, 42.03)
REQUIRED_PROVENANCE = (
    'fid', 'st', 'source_dataset', 'source_id', 'source_scale',
    'source_scale_status', 'source_ref', 'source_url', 'publication_id')
USER_AGENT = (
    'nw-mineral-monitor/11 Nevada state-survey PMTiles builder '
    '(official public research data)')

BASELINE_KEYS = {
    'nv_usgs_ds249': DS249_OUT,
    'nv_nbmg_onegeology_250k': ONEGEOLOGY_OUT,
    'nv_nbmg_mining_districts': DISTRICTS_OUT,
}

ARCGIS_SNAPSHOT_CONTRACTS = {
    'nv_nbmg_onegeology_250k': {
        'object_id_field': 'OBJECTID',
        'n': 54_389,
        'minimum_object_id': 1, 'maximum_object_id': 54_389,
        'object_ids_sha256':
            'a02716ca3f729cd69548fd9d92b18c0cf9fae53ab932f67287635d8df718082e',
        'layer_metadata_sha256':
            'f70c333929470c9ec97fbd3f878a0eb305765f9c56cb3925160afc0bb3dcedb1',
    },
    'nv_nbmg_mining_districts': {
        'object_id_field': 'OBJECTID_1',
        'n': 535,
        'minimum_object_id': 1, 'maximum_object_id': 535,
        'object_ids_sha256':
            '950ad17644d4bcc8555638339d4738964bfe85acb83beb1b222d1aceb2e0ec4e',
        'layer_metadata_sha256':
            '07e8525e5d520493b2f51bfe8b53820423a22850e118e72a99116e078de4f1ba',
    },
}
SPATIAL_CLIP_CONTRACTS = {
    'nv_nbmg_onegeology_250k': {
        'tiled_n': 52_017,
        'fully_outside_count': 2_370,
        'fully_outside_object_ids_sha256':
            '6bc1e4533a79a676f93e74faa019d6d2e596ddb56e09079518fc3fd390dd72f4',
        'geometry_clipped_count': 330,
        'geometry_clipped_object_ids_sha256':
            '5c13d2d25188ecf801d6e4be33543c2db0bece4bd5ef6a42f4f0552d90061aa9',
    },
    'nv_nbmg_mining_districts': {
        'tiled_n': 534,
        'fully_outside_count': 1,
        'fully_outside_object_ids_sha256':
            'e8a5394244ee6c507f1d64fcf87773387a322b00f20bbeb7f70f479d596db678',
        'geometry_clipped_count': 15,
        'geometry_clipped_object_ids_sha256':
            '2e02d9f99708235950e346cd9b7c5b17249bf42475e68ac798df61b6dc34dad3',
    },
}


def _text(value, limit=500):
    if value is None:
        return None
    value = re.sub(r'\s+', ' ', str(value)).strip()
    return value[:limit] if value else None


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_sha256(value):
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(',', ':'), allow_nan=False
    ).encode()).hexdigest()


def _ensure_private_staging_root():
    """Reject a public staging configuration before creating any directory."""
    site = os.path.realpath(SITE)
    staging = os.path.realpath(PRIVATE_STAGING_ROOT)
    try:
        inside_site = os.path.commonpath((site, staging)) == site
    except ValueError as exc:
        raise RuntimeError(
            'Nevada staging root and public site must share a resolvable path '
            'namespace') from exc
    if inside_site:
        raise RuntimeError('Nevada staging root must be outside public site/')
    os.makedirs(PRIVATE_STAGING_ROOT, exist_ok=True)


def _positive_oid(value, field='OBJECTID'):
    if isinstance(value, bool):
        raise RuntimeError(f'{field} must be a positive integer')
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f'{field} must be a positive integer') from exc
    if parsed <= 0 or str(value).strip() not in (str(parsed), f'{parsed}.0'):
        raise RuntimeError(f'{field} must be a positive integer')
    return parsed


def _ci_properties(feature):
    properties = feature.get('properties') or feature.get('attributes') or {}
    if not isinstance(properties, dict):
        raise RuntimeError('feature properties are not an object')
    return {str(key).casefold(): value for key, value in properties.items()}


def _positions(value):
    if (isinstance(value, (list, tuple)) and len(value) >= 2 and
            all(isinstance(item, (int, float)) and not isinstance(item, bool)
                for item in value[:2])):
        yield float(value[0]), float(value[1])
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from _positions(child)


def _plain_geometry(value, expected_types, *, forced_type=None):
    if hasattr(value, '__geo_interface__'):
        value = value.__geo_interface__
    if not isinstance(value, dict):
        try:
            value = dict(value)
        except (TypeError, ValueError) as exc:
            raise RuntimeError('feature geometry is not a mapping') from exc
    geometry_type = forced_type or value.get('type')
    if geometry_type not in expected_types:
        raise RuntimeError(
            f'feature geometry {geometry_type!r} is not one of '
            f'{sorted(expected_types)}')
    coordinates = value.get('coordinates')
    pairs = list(_positions(coordinates))
    minimum = 4 if geometry_type in ('Polygon', 'MultiPolygon') else 2
    if len(pairs) < minimum:
        raise RuntimeError('feature geometry has too few coordinate positions')
    for longitude, latitude in pairs:
        if (not math.isfinite(longitude) or not math.isfinite(latitude) or
                not NV_BOUNDS[0] <= longitude <= NV_BOUNDS[2] or
                not NV_BOUNDS[1] <= latitude <= NV_BOUNDS[3]):
            raise RuntimeError(
                f'Nevada feature has out-of-scope coordinate '
                f'({longitude}, {latitude})')
    # Round once more here because ArcGIS and Fiona models may expose tuples,
    # custom coordinate objects, or more precision than the request asked for.
    def convert(item):
        if (isinstance(item, (list, tuple)) and len(item) >= 2 and
                all(isinstance(part, (int, float)) and not isinstance(part, bool)
                    for part in item[:2])):
            return [round(float(item[0]), 6), round(float(item[1]), 6)]
        if not isinstance(item, (list, tuple)) or not item:
            raise RuntimeError('feature geometry has malformed coordinate nesting')
        return [convert(child) for child in item]
    return {'type': geometry_type, 'coordinates': convert(coordinates)}


def _load_nv_clip():
    if any(value is None for value in (
            shapely, shapely_make_valid, shapely_mapping, shapely_shape,
            shapely_unary_union, shapely_prepare, shapely_explain_validity)):
        raise RuntimeError('Shapely 2.x is required to clip NBMG service polygons')
    try:
        with open(STATE_CLIPS, 'rb') as source:
            raw = source.read()
        data = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f'authoritative state clip is unreadable: {exc}') from exc
    states = data.get('states') if isinstance(data, dict) else None
    source = data.get('source') if isinstance(data, dict) else None
    if (data.get('schema_version') != 1 or not isinstance(source, str) or
            'TIGERweb' not in source or 'January 1 2025' not in source or
            not isinstance(states, dict) or set(states) != set(ALL_STATES)):
        raise RuntimeError('state clip must be the exact Census 2025 49-state index')
    geometry = states.get('NV')
    if (not isinstance(geometry, dict) or
            geometry.get('type') not in ('Polygon', 'MultiPolygon')):
        raise RuntimeError('authoritative Nevada clip is not a polygon')
    boundary = shapely_shape(geometry)
    if boundary.is_empty or not boundary.is_valid:
        raise RuntimeError('authoritative Nevada clip is empty or invalid')
    return {
        'boundary': boundary, 'prepared': shapely_prepare(boundary),
        'manifest': {
            'authority': source, 'method': 'geometric intersection',
            'artifact': os.path.relpath(STATE_CLIPS, ROOT),
            'artifact_sha256': hashlib.sha256(raw).hexdigest(),
        },
    }


def _polygon_parts(geometry):
    if geometry.geom_type == 'Polygon':
        return [geometry]
    if geometry.geom_type == 'MultiPolygon':
        return list(geometry.geoms)
    if hasattr(geometry, 'geoms'):
        out = []
        for child in geometry.geoms:
            out.extend(_polygon_parts(child))
        return out
    return []


def _make_valid_polygon(geometry):
    """Return a valid polygonal shape and complete evidence for any repair."""
    source = (geometry if getattr(geometry, 'geom_type', None) else
              shapely_shape(geometry))
    if source.is_empty:
        return source, None
    repair = None
    work = source
    if not source.is_valid:
        raw_reason = shapely_explain_validity(source)
        reason = raw_reason.split('[', 1)[0].strip()
        repaired = shapely_make_valid(source)
        polygon_parts = [part for part in _polygon_parts(repaired)
                         if not part.is_empty and part.area > 0]
        nonpolygon = {}

        def atomic_parts(value):
            if hasattr(value, 'geoms'):
                for child in value.geoms:
                    yield from atomic_parts(child)
            else:
                yield value

        for part in atomic_parts(repaired):
            if part.geom_type != 'Polygon' and not part.is_empty:
                nonpolygon[part.geom_type] = nonpolygon.get(part.geom_type, 0) + 1
        if not polygon_parts:
            raise RuntimeError('make_valid produced no positive-area polygon parts')
        work = (polygon_parts[0] if len(polygon_parts) == 1 else
                shapely_unary_union(polygon_parts))
        if (work.is_empty or not work.is_valid or
                work.geom_type not in ('Polygon', 'MultiPolygon')):
            raise RuntimeError(
                f'make_valid produced invalid {work.geom_type!r} polygon output')
        source_area, repaired_area = float(source.area), float(work.area)
        absolute = abs(repaired_area - source_area)
        relative = absolute / max(source_area, 1e-30)
        repair = {
            'validity_reason': reason, 'validity_detail': raw_reason,
            'source_type': source.geom_type,
            'make_valid_type': repaired.geom_type,
            'polygon_output_type': work.geom_type,
            'polygon_parts': len(polygon_parts),
            'nonpolygon_parts_dropped_by_type': nonpolygon,
            'source_area': source_area,
            'repaired_area': repaired_area,
            'absolute_area_delta': absolute,
            'relative_area_delta': relative,
        }
    return work, repair


def _clip_polygon(geometry, clip):
    """Repair with recorded evidence, then intersect the authoritative clip."""
    work, repair = _make_valid_polygon(geometry)
    if work.is_empty:
        return None, {'changed': False, 'repair': repair, 'outside': True,
                      'preclip_area_square_degrees': 0.0,
                      'postclip_area_square_degrees': 0.0}
    changed = not clip['prepared'].covers(work)
    result = work.intersection(clip['boundary']) if changed else work
    parts = [part for part in _polygon_parts(result)
             if not part.is_empty and part.area > 0]
    if not parts:
        return None, {
            'changed': changed, 'repair': repair, 'outside': True,
            'preclip_area_square_degrees': float(work.area),
            'postclip_area_square_degrees': 0.0,
        }
    result = parts[0] if len(parts) == 1 else shapely_unary_union(parts)
    if (result.is_empty or not result.is_valid or
            result.geom_type not in ('Polygon', 'MultiPolygon')):
        raise RuntimeError(
            f'Nevada polygon clip produced invalid {result.geom_type!r}')
    return shapely_mapping(result), {
        'changed': changed, 'repair': repair, 'outside': False,
        'preclip_area_square_degrees': float(work.area),
        'postclip_area_square_degrees': float(result.area),
    }


def _request_json(url, params=None, *, post=False, tries=6):
    params = params or {}
    encoded = urllib.parse.urlencode(params).encode('ascii')
    headers = {'User-Agent': USER_AGENT, 'Accept': 'application/json'}
    if post:
        headers['Content-Type'] = 'application/x-www-form-urlencoded'
        request = urllib.request.Request(url, data=encoded, headers=headers)
    else:
        suffix = ('?' + encoded.decode('ascii')) if encoded else ''
        request = urllib.request.Request(url + suffix, headers=headers)
    last = None
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(request, timeout=180) as response:
                value = json.load(response)
            error = value.get('error') if isinstance(value, dict) else None
            if not error:
                return value
            last = RuntimeError(f'ArcGIS error from {url}: {error}')
            code = error.get('code') if isinstance(error, dict) else None
            if code not in (429, 500, 502, 503, 504):
                raise last
        except urllib.error.HTTPError as exc:
            last = exc
            if exc.code not in (429, 500, 502, 503, 504):
                raise RuntimeError(
                    f'ArcGIS request failed: HTTP {exc.code} ({url})') from exc
        except (urllib.error.URLError, TimeoutError,
                json.JSONDecodeError) as exc:
            last = exc
        if attempt + 1 < tries:
            time.sleep(min(30, 2 ** attempt))
    raise RuntimeError(f'ArcGIS request failed after {tries} attempts: {last}')


def _layer_snapshot(layer, expected_name, expected_geometry, scale_text,
                    *, scale_in_layer_metadata=True):
    metadata = _request_json(layer, {'f': 'json'})
    if (metadata.get('name') != expected_name or
            metadata.get('geometryType') != expected_geometry or
            (scale_in_layer_metadata and
             scale_text.casefold() not in
             str(metadata.get('description') or '').casefold())):
        raise RuntimeError(f'{layer} metadata identity/scale contract changed')
    ids_result = _request_json(f'{layer}/query', {
        'where': '1=1', 'returnIdsOnly': 'true', 'f': 'json'})
    field = ids_result.get('objectIdFieldName')
    raw_ids = ids_result.get('objectIds')
    if not isinstance(field, str) or not field or not isinstance(raw_ids, list):
        raise RuntimeError(f'{layer} returned no valid object-ID snapshot')
    ids = sorted(_positive_oid(value, field) for value in raw_ids)
    if not ids or len(ids) != len(set(ids)):
        raise RuntimeError(f'{layer} object-ID snapshot is empty or duplicated')
    selected_metadata = {
        'name': metadata['name'], 'geometryType': metadata['geometryType'],
        'description': metadata.get('description'),
        'maxRecordCount': metadata.get('maxRecordCount'),
        'fields': [
            {'name': item.get('name'), 'type': item.get('type')}
            for item in metadata.get('fields') or []
            if isinstance(item, dict)
        ],
    }
    return {
        'oid_field': field, 'ids': ids,
        'id_sha256': _canonical_sha256(ids),
        'metadata_sha256': _canonical_sha256(selected_metadata),
        'metadata': selected_metadata,
    }


def _assert_arcgis_snapshot_contract(baseline_id, snapshot):
    contract = ARCGIS_SNAPSHOT_CONTRACTS[baseline_id]
    ids = snapshot.get('ids') or []
    observed = {
        'object_id_field': snapshot.get('oid_field'),
        'n': len(ids),
        'minimum_object_id': ids[0] if ids else None,
        'maximum_object_id': ids[-1] if ids else None,
        'object_ids_sha256': snapshot.get('id_sha256'),
        'layer_metadata_sha256': snapshot.get('metadata_sha256'),
    }
    if observed != contract:
        raise RuntimeError(
            f'{baseline_id} live ArcGIS snapshot changed; review is required: '
            f'expected={contract}, observed={observed}')


def _iter_snapshot(layer, snapshot, fields, page=250):
    oid_field, ids = snapshot['oid_field'], snapshot['ids']
    emitted = 0
    label = '/'.join(layer.rstrip('/').split('/')[-2:])
    print(f'{label}: pinned {len(ids):,} object IDs')
    for start in range(0, len(ids), page):
        expected = ids[start:start + page]
        data = _request_json(f'{layer}/query', {
            'objectIds': ','.join(map(str, expected)),
            'outFields': ','.join(dict.fromkeys((oid_field, *fields))),
            'returnGeometry': 'true', 'returnTrueCurves': 'false',
            'outSR': 4326, 'geometryPrecision': 6,
            'orderByFields': f'{oid_field} ASC', 'f': 'geojson',
        }, post=True)
        features = data.get('features')
        if not isinstance(features, list):
            raise RuntimeError(f'{layer} snapshot page has no feature array')
        actual = [
            _positive_oid(_ci_properties(feature).get(oid_field.casefold()),
                          oid_field)
            for feature in features
        ]
        if actual != expected:
            raise RuntimeError(
                f'{layer} snapshot page mismatch at {start}: '
                f'expected {expected[:3]}..{expected[-3:]}, '
                f'got {actual[:3]}..{actual[-3:]}')
        yield from features
        emitted += len(features)
        if emitted % 10_000 < page or emitted == len(ids):
            print(f'{label}: {emitted:,}/{len(ids):,}')
    if emitted != len(ids):
        raise RuntimeError(f'{layer} emitted {emitted:,}; snapshot has {len(ids):,}')


def _base_properties(*, fid, dataset, source_id, scale, scale_status,
                     reference, source_url, publication_id):
    return {
        'fid': fid, 'st': 'NV', 'source_dataset': dataset,
        'source_id': source_id, 'source_scale': scale,
        'source_scale_status': scale_status,
        'source_ref': reference, 'source_url': source_url,
        'publication_id': publication_id,
    }


def _normalize_onegeology(feature):
    p = _ci_properties(feature)
    oid = _positive_oid(p.get('objectid'))
    props = _base_properties(
        fid=oid, dataset='nbmg_onegeology_2013_250k',
        source_id=f'nbmg-onegeology-250k:{oid}', scale='1:250,000',
        scale_status='arcgis_layer_metadata',
        reference=('NBMG, 2013, OneGeology conversion, '
                   'US-NV_NBMG_250k_Geology'),
        source_url=NBMG_GEOLOGY, publication_id='NBMG OneGeology 2013 250k')
    props.update({
        'source_record_id': str(oid), 'county_source': _text(p.get('county'), 50),
        'unit_id': _text(p.get('identifier'), 100),
        'unit_name': _text(p.get('name'), 200),
        'unit_description': _text(p.get('description'), 250),
        'unit_type': _text(p.get('geologicunittype'), 100),
        'lithology': _text(p.get('lithology'), 160),
        'age': _text(p.get('geologichistory'), 120),
        'observation_method': _text(p.get('observationmethod'), 120),
        'positional_accuracy': _text(p.get('positionalaccuracy'), 120),
        'source_field': _text(p.get('source'), 240),
        'symbol': _text(p.get('genericsymbolizer'), 40),
    })
    return {
        'type': 'Feature', 'id': oid,
        'properties': {key: value for key, value in props.items()
                       if value is not None},
        'geometry': _plain_geometry(
            feature.get('geometry'), {'Polygon', 'MultiPolygon'}),
    }


def _normalize_district(feature):
    p = _ci_properties(feature)
    oid = _positive_oid(p.get('objectid_1'), 'OBJECTID_1')
    props = _base_properties(
        fid=oid, dataset='nbmg_report47_live_gis',
        source_id=f'nbmg-report47-live:{oid}', scale='1:1,000,000',
        scale_status='report47_publication_metadata',
        reference=('Tingley, J.V., 1998, Mining districts of Nevada, '
                   'second edition, NBMG Report 47z'),
        source_url=NBMG_DISTRICTS,
        publication_id='NBMG Report 47z (1998)')
    props.update({
        'source_record_id': str(oid),
        'district_id': _text(p.get('district_no'), 30),
        'district_name': _text(p.get('district_name'), 100),
        'district_type': _text(p.get('district_type'), 40),
    })
    if not props['district_id'] or not props['district_name']:
        raise RuntimeError(f'NBMG Report 47 row {oid} lacks district identity')
    return {
        'type': 'Feature', 'id': oid,
        'properties': props,
        'geometry': _plain_geometry(
            feature.get('geometry'), {'Polygon', 'MultiPolygon'}),
    }


def _write_feature(output, feature):
    output.write(json.dumps(feature, separators=(',', ':'), allow_nan=False))
    output.write('\n')


def _download_ds249(path):
    request = urllib.request.Request(DS249_URL, headers={'User-Agent': USER_AGENT})
    digest = hashlib.sha256()
    size = 0
    try:
        with urllib.request.urlopen(request, timeout=300) as response, \
                open(path, 'wb') as output:
            for block in iter(lambda: response.read(1024 * 1024), b''):
                output.write(block)
                digest.update(block)
                size += len(block)
    except (OSError, urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError(f'DS 249 download failed: {exc}') from exc
    sha256 = digest.hexdigest()
    if size != DS249_BYTES or sha256 != DS249_SHA256:
        raise RuntimeError(
            'DS 249 bulk package changed; expected reviewed '
            f'{DS249_BYTES} bytes/{DS249_SHA256}, got {size}/{sha256}')
    return {'bytes': size, 'sha256': sha256}


def _extract_ds249(archive_path, directory):
    required = tuple(member for members in DS249_MEMBERS.values()
                     for member in members)
    paths = {}
    try:
        with zipfile.ZipFile(archive_path) as archive:
            names = archive.namelist()
            if len(names) != len(set(names)):
                raise RuntimeError('DS 249 ZIP contains duplicate member names')
            missing = sorted(set(required) - set(names))
            if missing:
                raise RuntimeError(f'DS 249 ZIP lacks required members: {missing}')
            total = sum(archive.getinfo(name).file_size for name in required)
            if not 1_000_000 < total < 500_000_000:
                raise RuntimeError(f'DS 249 extraction size is implausible: {total}')
            for name in required:
                target = os.path.join(directory, os.path.basename(name))
                with archive.open(name) as source, open(target, 'wb') as output:
                    shutil.copyfileobj(source, output)
                paths[os.path.basename(name)] = target
    except (OSError, zipfile.BadZipFile) as exc:
        raise RuntimeError(f'DS 249 package is not a valid ZIP: {exc}') from exc
    return {
        'geology': paths['NevadaGeology.shp'],
        'faults': paths['StatewideFaults.shp'],
    }


def _ds249_properties(kind, fid, properties):
    citation = ('Crafford, A.E.J., 2007, Geologic map of Nevada, '
                'U.S. Geological Survey Data Series 249, version 1.1')
    base = _base_properties(
        fid=fid, dataset='usgs_ds249_v1_1',
        source_id=f'usgs-ds249:{kind}:{fid}', scale='1:250,000',
        scale_status='publication_metadata', reference=citation,
        source_url=DS249_DOI, publication_id='USGS DS 249 v1.1')
    p = {str(key).casefold(): value for key, value in properties.items()}
    base['source_record_id'] = str(fid - 1)
    if kind == 'geology':
        base.update({
            'unit_label': _text(p.get('geologicfm'), 30),
            'source_label': _text(p.get('fmatn'), 30),
            'state_map_label': _text(p.get('statemap'), 30),
            'unit_name': _text(p.get('l_name'), 160),
            'county_source': _text(p.get('county'), 60),
            'notes': _text(p.get('notes'), 250),
            'source_refs_field': _text(p.get('refs'), 80),
            'reviewed_field': _text(p.get('reviewed'), 20),
        })
    else:
        base.update({
            'fault_type': _text(p.get('f_type'), 80) or 'unspecified fault',
            'fault_code': p.get('f_code'),
            'fault_movement': _text(p.get('f_move'), 10),
        })
    return {key: value for key, value in base.items() if value is not None}


def _line_coordinate_count(geometry):
    if geometry.geom_type == 'LineString':
        return len(geometry.coords)
    return sum(len(part.coords) for part in geometry.geoms)


def _fault_encoding_exclusion_evidence(records):
    contract = DS249_FAULT_ENCODING_EXCLUSION_CONTRACT
    fids = [record['fid'] for record in records]
    source_ids = [record['source_record_id'] for record in records]
    evidence = {
        'status': 'reviewed_below_encoding_scale_exclusion',
        'method': 'no_geometry_fabrication_source_record_omitted',
        'reason_code': contract['reason'],
        'reason': (
            'Valid two-vertex source traces shorter than 2 m in the '
            '1:250,000 compilation collapse under z12 MVT quantization; '
            'they are explicitly inventoried instead of silently dropped or '
            'artificially lengthened.'),
        'source_scale': '1:250,000',
        'native_crs': 'EPSG:26711',
        'tippecanoe_version': contract['tippecanoe_version'],
        'tippecanoe_maxzoom': contract['tippecanoe_maxzoom'],
        'tippecanoe_full_detail': contract['tippecanoe_full_detail'],
        'count': len(records),
        'fids': fids, 'fids_sha256': _canonical_sha256(fids),
        'source_record_ids': source_ids,
        'source_record_ids_sha256': _canonical_sha256(source_ids),
        'records': records, 'records_sha256': _canonical_sha256(records),
        'source_geometry_hashes_sha256': _canonical_sha256(
            [record['source_geometry_sha256'] for record in records]),
        'minimum_native_length_m': min(
            (record['native_length_m'] for record in records), default=0.0),
        'maximum_native_length_m': max(
            (record['native_length_m'] for record in records), default=0.0),
        'sum_native_length_m': sum(
            record['native_length_m'] for record in records),
        'maximum_accepted_native_length_m':
            contract['maximum_accepted_native_length_m'],
    }
    expected = {
        field: contract[field] for field in (
            'count', 'fids', 'fids_sha256', 'source_record_ids',
            'source_record_ids_sha256', 'records_sha256',
            'source_geometry_hashes_sha256', 'minimum_native_length_m',
            'maximum_native_length_m', 'sum_native_length_m',
            'maximum_accepted_native_length_m', 'tippecanoe_version',
            'tippecanoe_maxzoom', 'tippecanoe_full_detail')
    }
    observed = {field: evidence[field] for field in expected}
    if observed != expected:
        raise RuntimeError(
            'DS 249 fault below-encoding-scale inventory changed: '
            f'expected={expected}, observed={observed}')
    if any(
            record['source_type'] != 'LineString' or
            record['coordinate_count'] != 2 or
            not 0 < record['native_length_m'] <=
            contract['maximum_accepted_native_length_m'] or
            re.fullmatch(r'[0-9a-f]{64}',
                         record['source_geometry_sha256']) is None
            for record in records):
        raise RuntimeError(
            'DS 249 fault encoding exclusion contains an unreviewed geometry')
    if ([record['native_length_m'] for record in records] !=
            contract['native_lengths_m'] or
            [record['source_geometry_sha256'] for record in records] !=
            contract['source_geometry_sha256']):
        raise RuntimeError(
            'DS 249 fault encoding-exclusion source geometry changed')
    return evidence


def _stream_ds249(paths, geology_sequence, faults_sequence):
    if fiona is None or transform_geom is None:
        raise RuntimeError(
            'Fiona with coordinate-transform support is required for DS 249; '
            'run this builder with the project geospatial Python environment')
    specs = {
        'geology': {
            'path': paths['geology'], 'sequence': geology_sequence,
            'schema_geometry': 'Polygon',
            'geometry_types': {'Polygon', 'MultiPolygon'},
            'fields': {'GEOLOGICFM', 'FMATN', 'L_NAME', 'County'},
        },
        'faults': {
            'path': paths['faults'], 'sequence': faults_sequence,
            'schema_geometry': 'LineString',
            'geometry_types': {'LineString', 'MultiLineString'},
            'fields': {'F_TYPE', 'F_CODE'},
        },
    }
    result = {}
    for kind, spec in specs.items():
        nulls = []
        emitted = 0
        geometry_type_ids = {
            geometry_type: [] for geometry_type in spec['geometry_types']}
        repair_records = []
        reason_counts = {}
        transition_counts = {}
        dropped = {}
        invalid_nonpolygon = []
        encoding_exclusion_records = []
        with fiona.open(spec['path']) as source, \
                open(spec['sequence'], 'w', encoding='utf-8') as output:
            epsg = source.crs.to_epsg() if hasattr(source.crs, 'to_epsg') else None
            source_schema = source.schema.get('geometry')
            if (epsg != 26711 or
                    source_schema != spec['schema_geometry']):
                raise RuntimeError(
                    f'DS 249 {kind} CRS/geometry changed: '
                    f'{source.crs!r}/{source_schema!r}')
            if not spec['fields'] <= set(source.schema.get('properties') or {}):
                raise RuntimeError(f'DS 249 {kind} schema lacks required fields')
            source_records = len(source)
            for index, raw in enumerate(source):
                record_id = str(raw.id)
                if raw.geometry is None:
                    nulls.append(record_id)
                    continue
                native = shapely_shape(raw.geometry)
                source_type = native.geom_type
                if source_type not in spec['geometry_types']:
                    raise RuntimeError(
                        f'DS 249 {kind} record {record_id} has unreviewed '
                        f'geometry type {source_type!r}')
                if native.is_empty:
                    raise RuntimeError(
                        f'DS 249 {kind} record {record_id} has an unreviewed '
                        'non-null empty geometry')
                geometry_type_ids[source_type].append(record_id)

                output_geometry = native
                if kind == 'geology':
                    output_geometry, repair = _make_valid_polygon(native)
                    if repair is not None:
                        transition = '->'.join((
                            repair['source_type'], repair['make_valid_type'],
                            repair['polygon_output_type']))
                        transition_counts[transition] = (
                            transition_counts.get(transition, 0) + 1)
                        reason = repair['validity_reason']
                        reason_counts[reason] = reason_counts.get(reason, 0) + 1
                        for geometry_type, part_count in \
                                repair['nonpolygon_parts_dropped_by_type'].items():
                            dropped[geometry_type] = (
                                dropped.get(geometry_type, 0) + part_count)
                        repair_records.append({
                            'object_id': record_id,
                            'validity_reason': repair['validity_reason'],
                            'validity_detail': repair['validity_detail'],
                            'source_type': repair['source_type'],
                            'make_valid_type': repair['make_valid_type'],
                            'polygon_output_type': repair['polygon_output_type'],
                            'polygon_parts': repair['polygon_parts'],
                            'nonpolygon_parts_dropped_by_type':
                                repair['nonpolygon_parts_dropped_by_type'],
                            'source_area': repair['source_area'],
                            'repaired_area': repair['repaired_area'],
                            'absolute_area_delta': repair['absolute_area_delta'],
                            'relative_area_delta': repair['relative_area_delta'],
                        })
                elif not native.is_valid:
                    invalid_nonpolygon.append({
                        'object_id': record_id,
                        'validity_detail': shapely_explain_validity(native),
                    })

                fid = index + 1
                if (kind == 'faults' and
                        fid in DS249_FAULT_ENCODING_EXCLUSION_CONTRACT['fids']):
                    encoding_exclusion_records.append({
                        'fid': fid, 'source_record_id': record_id,
                        'source_type': native.geom_type,
                        'coordinate_count': _line_coordinate_count(native),
                        'native_length_m': float(native.length),
                        'source_geometry_sha256': _canonical_sha256(
                            shapely_mapping(native)['coordinates']),
                    })
                    continue
                transformed = transform_geom(
                    source.crs, 'EPSG:4326',
                    shapely_mapping(output_geometry), precision=6)
                # Fiona 1.9.x can omit the type on a transformed legacy ESRI
                # geometry, while retaining the correctly transformed nesting.
                coordinates = (transformed.get('coordinates')
                               if hasattr(transformed, 'get') else
                               transformed.__geo_interface__.get('coordinates'))
                output_type = output_geometry.geom_type
                geometry = _plain_geometry(
                    {'type': output_type, 'coordinates': coordinates},
                    spec['geometry_types'], forced_type=output_type)
                properties = _ds249_properties(
                    kind, fid, dict(raw.properties))
                _write_feature(output, {
                    'type': 'Feature', 'id': fid,
                    'properties': properties, 'geometry': geometry})
                emitted += 1
        if nulls != DS249_NULL_GEOMETRY[kind]:
            raise RuntimeError(
                f'DS 249 {kind} null-geometry inventory changed: {nulls}')
        if (source_records != emitted + len(nulls) +
                len(encoding_exclusion_records) or emitted <= 0):
            raise RuntimeError(f'DS 249 {kind} source/emitted counts do not reconcile')
        geometry_inventory = {
            'source_schema': source_schema,
            'by_type': {
                geometry_type: len(geometry_type_ids[geometry_type])
                for geometry_type in sorted(geometry_type_ids)},
            'by_type_object_ids_sha256': {
                geometry_type: _canonical_sha256(
                    geometry_type_ids[geometry_type])
                for geometry_type in sorted(geometry_type_ids)},
        }
        if geometry_inventory != DS249_GEOMETRY_CONTRACT[kind]:
            raise RuntimeError(
                f'DS 249 {kind} per-record geometry inventory changed: '
                f'expected={DS249_GEOMETRY_CONTRACT[kind]}, '
                f'observed={geometry_inventory}')
        if invalid_nonpolygon:
            raise RuntimeError(
                f'DS 249 {kind} has unreviewed invalid source geometry: '
                f'{invalid_nonpolygon}')
        repair_contract = (DS249_GEOLOGY_REPAIR_CONTRACT
                           if kind == 'geology' else
                           DS249_FAULT_REPAIR_CONTRACT)
        topology_repair = _repair_evidence(
            repair_records, reason_counts, transition_counts, dropped,
            repair_contract,
            ordering=('validate_then_make_valid_in_epsg26711_then_epsg4326_transform'
                      if kind == 'geology' else
                      'validate_in_epsg26711_then_epsg4326_transform'),
            area_units='square meters in EPSG:26711')
        encoding_exclusions = (
            _fault_encoding_exclusion_evidence(encoding_exclusion_records)
            if kind == 'faults' else None)
        result[kind] = {
            'source_records': source_records, 'n': emitted,
            'null_geometry_count': len(nulls),
            'null_geometry_source_record_ids': nulls,
            'geometry_inventory': geometry_inventory,
            'topology_repair': topology_repair,
            'encoding_exclusions': encoding_exclusions,
        }
        print(f'DS 249 {kind}: {emitted:,} tiled; {len(nulls)} null geometry')
    return result


def _repair_evidence(records, reason_counts, transition_counts, dropped,
                     contract, *, ordering, area_units):
    ids = [record['object_id'] for record in records]
    max_absolute = max(
        ((record['absolute_area_delta'], record['object_id'])
         for record in records), default=(0.0, None))
    max_relative = max(
        ((record['relative_area_delta'], record['object_id'])
         for record in records), default=(0.0, None))
    evidence = {
        'status': ('reviewed_pinned_source_repair' if records else
                   'reviewed_pinned_source_no_repair_required'),
        'ordering': ordering,
        'method': ('GEOSMakeValid via shapely.make_valid' if records else None),
        'shapely_version': shapely.__version__,
        'geos_version': shapely.geos_version_string,
        'count': len(records), 'object_ids': ids,
        'object_ids_sha256': _canonical_sha256(ids),
        'records_sha256': _canonical_sha256(records),
        'validity_reason_counts': reason_counts,
        'type_transition_counts': transition_counts,
        'nonpolygon_parts_dropped_by_type': dropped,
        'area_delta': {
            'units': area_units,
            'maximum_absolute': {
                'value': max_absolute[0], 'object_id': max_absolute[1],
                'acceptance_ceiling': contract['max_absolute_area_delta'],
            },
            'maximum_relative': {
                'value': max_relative[0], 'object_id': max_relative[1],
                'acceptance_ceiling': contract['max_relative_area_delta'],
            },
            'sum_absolute': sum(
                record['absolute_area_delta']
                for record in records),
        },
    }
    expected = {
        'count': contract['count'],
        'object_ids_sha256': contract['object_ids_sha256'],
        'validity_reason_counts': contract['validity_reason_counts'],
        'type_transition_counts': contract['type_transition_counts'],
        'nonpolygon_parts_dropped_by_type':
            contract['nonpolygon_parts_dropped_by_type'],
        'shapely_version': contract['shapely_version'],
        'geos_version': contract['geos_version'],
    }
    observed = {key: evidence[key] for key in expected}
    if observed != expected:
        raise RuntimeError(
            f'invalid-geometry repair contract changed: expected={expected}, '
            f'observed={observed}')
    for field in ('maximum_absolute', 'maximum_relative'):
        metric = evidence['area_delta'][field]
        if (not isinstance(metric['value'], (int, float)) or
                not math.isfinite(metric['value']) or metric['value'] < 0 or
                metric['value'] > metric['acceptance_ceiling']):
            raise RuntimeError(
                f'invalid-geometry repair {field} area delta exceeds '
                f'reviewed ceiling: {metric}')
    return evidence


def _stream_arcgis(layer, snapshot, fields, normalize, sequence,
                   *, clip=None, expected_empty_geometry_ids=(),
                   expected_fully_outside_ids=None, repair_contract=None):
    count = 0
    empty = []
    fully_outside = []
    clipped_ids = []
    repair_records = []
    reason_counts = {}
    transition_counts = {}
    dropped = {}
    preclip_area = 0.0
    postclip_area = 0.0
    with open(sequence, 'w', encoding='utf-8') as output:
        for raw in _iter_snapshot(layer, snapshot, fields):
            p = _ci_properties(raw)
            oid = _positive_oid(p.get(snapshot['oid_field'].casefold()),
                                snapshot['oid_field'])
            coordinates = (raw.get('geometry') or {}).get('coordinates')
            if not list(_positions(coordinates)):
                empty.append(oid)
                continue
            if clip is not None:
                clipped_geometry, flags = _clip_polygon(raw['geometry'], clip)
                repair = flags['repair']
                if repair is not None:
                    transition = '->'.join((
                        repair['source_type'], repair['make_valid_type'],
                        repair['polygon_output_type']))
                    transition_counts[transition] = (
                        transition_counts.get(transition, 0) + 1)
                    reason = repair['validity_reason']
                    reason_counts[reason] = reason_counts.get(reason, 0) + 1
                    for geometry_type, part_count in \
                            repair['nonpolygon_parts_dropped_by_type'].items():
                        dropped[geometry_type] = dropped.get(geometry_type, 0) + part_count
                    repair_records.append({
                        'object_id': oid,
                        'validity_reason': repair['validity_reason'],
                        'validity_detail': repair['validity_detail'],
                        'source_type': repair['source_type'],
                        'make_valid_type': repair['make_valid_type'],
                        'polygon_output_type': repair['polygon_output_type'],
                        'polygon_parts': repair['polygon_parts'],
                        'nonpolygon_parts_dropped_by_type':
                            repair['nonpolygon_parts_dropped_by_type'],
                        'source_area': repair['source_area'],
                        'repaired_area': repair['repaired_area'],
                        'absolute_area_delta': repair['absolute_area_delta'],
                        'relative_area_delta': repair['relative_area_delta'],
                    })
                preclip_area += flags['preclip_area_square_degrees']
                postclip_area += flags['postclip_area_square_degrees']
                if flags['outside']:
                    fully_outside.append(oid)
                    continue
                if flags['changed']:
                    clipped_ids.append(oid)
                raw = dict(raw)
                raw['geometry'] = clipped_geometry
            _write_feature(output, normalize(raw))
            count += 1
    if empty != list(expected_empty_geometry_ids):
        raise RuntimeError(
            f'{layer} empty-geometry inventory changed: {empty}')
    # Official adjoining-sheet features are expected in the NBMG services and
    # are legitimately removed by the authoritative state intersection. Their
    # complete current ID list is source evidence, not a hard-coded invariant.
    # Callers may still pin a reviewed list when a source promises state-only
    # geometry.
    if (expected_fully_outside_ids is not None and
            fully_outside != list(expected_fully_outside_ids)):
        raise RuntimeError(
            f'{layer} fully-outside inventory changed: {fully_outside}')
    if repair_contract is None:
        if repair_records:
            raise RuntimeError(
                f'{layer} contains unreviewed invalid geometry repairs')
        topology_repair = None
    else:
        topology_repair = _repair_evidence(
            repair_records, reason_counts, transition_counts, dropped,
            repair_contract,
            ordering='validate_then_make_valid_then_state_intersection',
            area_units='square degrees in EPSG:4326')
    if count + len(empty) + len(fully_outside) != len(snapshot['ids']) or count <= 0:
        raise RuntimeError(f'{layer} normalized count does not match ID snapshot')
    clip_evidence = {
        'ordering': 'topology_repair_before_state_intersection',
        'fully_outside_count': len(fully_outside),
        'fully_outside_object_ids': fully_outside,
        'fully_outside_object_ids_sha256': _canonical_sha256(fully_outside),
        'geometry_clipped_count': len(clipped_ids),
        'geometry_clipped_object_ids': clipped_ids,
        'geometry_clipped_object_ids_sha256': _canonical_sha256(clipped_ids),
        'geometry_unchanged_count': count - len(clipped_ids),
        'preclip_area_square_degrees': preclip_area,
        'postclip_area_square_degrees': postclip_area,
        'area_removed_square_degrees': preclip_area - postclip_area,
    }
    return {
        'source_records': len(snapshot['ids']), 'n': count,
        'empty_geometry_count': len(empty),
        'empty_geometry_object_ids': empty,
        'topology_repair': topology_repair,
        'spatial_clip': clip_evidence,
    }


def _run_tippecanoe(output, layers, attribution, maxzoom):
    command = [
        'tippecanoe', '--force', '--output', output,
        '--minimum-zoom=0', f'--maximum-zoom={maxzoom}',
        '--full-detail=12',
        '--no-feature-limit', '--no-tile-size-limit',
        '--no-tiny-polygon-reduction-at-maximum-zoom',
        '--simplify-only-low-zooms', '--read-parallel', '--quiet',
        f'--attribution={attribution}',
    ]
    for layer, sequence in layers:
        command.extend(('-L', f'{layer}:{sequence}'))
    subprocess.run(command, check=True)


def _tippecanoe_version():
    try:
        result = subprocess.run(
            ['tippecanoe', '--version'], check=True, capture_output=True,
            text=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RuntimeError(f'tippecanoe version check failed: {exc}') from exc
    output = (result.stdout + result.stderr).strip()
    match = re.fullmatch(r'tippecanoe (v\d+\.\d+\.\d+)', output)
    if match is None:
        raise RuntimeError(
            f'tippecanoe version output is unrecognized: {output!r}')
    return match.group(1)


def _validate_pmtiles(path, layers, *, pmtiles_header=None):
    if pmtiles_header is None:
        from validate_national import _pmtiles_header as pmtiles_header
    requirements = {layer: list(REQUIRED_PROVENANCE) for layer in layers}
    meta = pmtiles_header(
        path, list(layers), requirements, verify_feature_properties=True,
        expected_state='NV', expected_bounds=[NV_BOUNDS],
        collect_feature_ids=True)
    if set(meta['source_layers']) != set(layers):
        raise RuntimeError(
            f'{path} has unexpected source layers {meta["source_layers"]}')
    bounds = meta['bounds']
    if (bounds[0] < NV_BOUNDS[0] or bounds[1] < NV_BOUNDS[1] or
            bounds[2] > NV_BOUNDS[2] or bounds[3] > NV_BOUNDS[3]):
        raise RuntimeError(f'{path} PMTiles bounds escape Nevada: {bounds}')
    if any(meta['semantic_layer_counts'].get(layer, 0) <= 0 for layer in layers):
        raise RuntimeError(f'{path} has an empty source layer after full MVT scan')
    return meta


def _artifact_fields(path, meta):
    return {
        'bytes': os.path.getsize(path), 'sha256': _sha256(path),
        'bounds': meta['bounds'],
        'semantic_tile_feature_counts': meta['semantic_layer_counts'],
    }


def _ds249_entry(stats, download, artifact):
    total = sum(item['n'] for item in stats.values())
    excluded = sum(
        (item.get('encoding_exclusions') or {}).get('count', 0)
        for item in stats.values())
    tileable_source_records = total + excluded
    raw_source_records = sum(item['source_records'] for item in stats.values())
    return {
        'schema_version': 1, 'status': 'baseline_not_release', 'state': 'NV',
        'file': 'data/tiles/states/nv/usgs-ds249.pmtiles',
        'format': 'pmtiles',
        'source_layers': ['nv_ds249_geology', 'nv_ds249_faults'],
        'source': {
            'title': 'Geologic map of Nevada, version 1.1',
            'authority': 'U.S. Geological Survey; prepared with NBMG',
            'publication_id': 'USGS Data Series 249', 'doi': DS249_DOI,
            'bulk_url': DS249_URL, 'bulk_bytes': download['bytes'],
            'bulk_sha256': download['sha256'],
            'bulk_last_modified': DS249_LAST_MODIFIED,
            'native_crs': 'EPSG:26711', 'source_scale': '1:250,000',
        },
        # n is the unique browser-addressable source-feature count. Source
        # records that cannot exist at the declared MVT encoding resolution
        # are reconciled explicitly below, never included in n.
        'n': total, 'states': {'NV': total},
        'source_records': tileable_source_records,
        'raw_source_records': raw_source_records,
        'null_geometry_count': raw_source_records - tileable_source_records,
        'encoding_exclusion_count': excluded,
        'by_layer': stats,
        'retrieved': TODAY,
        'required_properties': list(REQUIRED_PROVENANCE),
        'provenance_note': (
            'Source and scale are stamped on every polygon/line from the '
            'reviewed DS 249 publication and checksum-pinned ZIP. Per-record '
            'geometry types are inventoried because the ESRI layer schema '
            'understates multipart features. Reviewed invalid geology is '
            'repaired in native EPSG:26711 before reprojection, with exact '
            'repair and area-delta evidence. Two zero-area source polygons '
            'have null geometry and are inventoried explicitly rather than '
            'silently dropped. Fifteen valid sub-2-meter fault traces below '
            'the z12 MVT encoding resolution are likewise exact-inventoried; '
            'they are not counted as rendered features or fabricated into '
            'longer faults.'),
        **artifact,
    }


def _arcgis_snapshot_manifest(snapshot):
    ids = snapshot['ids']
    return {
        'object_id_field': snapshot['oid_field'], 'n': len(ids),
        'minimum_object_id': ids[0], 'maximum_object_id': ids[-1],
        'object_ids_sha256': snapshot['id_sha256'],
        'layer_metadata_sha256': snapshot['metadata_sha256'],
    }


def _onegeology_entry(snapshot, stats, clip_manifest, artifact):
    n = stats['n']
    source_inventory = {key: value for key, value in stats.items()
                        if key != 'spatial_clip'}
    spatial_clip = dict(clip_manifest)
    spatial_clip.update(stats['spatial_clip'])
    return {
        'schema_version': 1, 'status': 'baseline_not_release', 'state': 'NV',
        'file': 'data/tiles/states/nv/nbmg-onegeology-250k.pmtiles',
        'format': 'pmtiles', 'source_layer': 'nv_nbmg_onegeology_250k',
        'source': {
            'title': 'NBMG OneGeology 1:250,000 geology conversion',
            'authority': 'Nevada Bureau of Mines and Geology',
            'service_layer': NBMG_GEOLOGY, 'catalog_url': NBMG_GEOLOGY_CATALOG,
            'publication_id': 'NBMG OneGeology 2013 250k',
            'source_scale': '1:250,000',
            'distinction': (
                'This live NBMG conversion is not labeled as the DS 249 bulk '
                'shapefile; DS 249 is published in its own archive.'),
        },
        'n': n, 'states': {'NV': n}, 'retrieved': TODAY,
        'snapshot': _arcgis_snapshot_manifest(snapshot),
        'source_inventory': source_inventory,
        'spatial_clip': spatial_clip,
        'required_properties': list(REQUIRED_PROVENANCE),
        'provenance_note': (
            'Every polygon carries the live service object ID, NBMG conversion '
            'publication identity, and the layer-metadata scale.'),
        **artifact,
    }


def _district_entry(snapshot, stats, clip_manifest, artifact):
    n = stats['n']
    source_inventory = {key: value for key, value in stats.items()
                        if key != 'spatial_clip'}
    spatial_clip = dict(clip_manifest)
    spatial_clip.update(stats['spatial_clip'])
    return {
        'schema_version': 1, 'status': 'baseline_not_release', 'state': 'NV',
        'file': 'data/tiles/states/nv/nbmg-report47-districts.pmtiles',
        'format': 'pmtiles', 'source_layer': 'nv_nbmg_mining_districts',
        'source': {
            'title': 'Mining districts of Nevada, second edition',
            'authority': 'Nevada Bureau of Mines and Geology',
            'author': 'Joseph V. Tingley', 'year': 1998,
            'publication_id': 'NBMG Report 47z',
            'service_layer': NBMG_DISTRICTS,
            'catalog_url': NBMG_DISTRICTS_CATALOG,
            'source_scale': '1:1,000,000',
        },
        'n': n, 'states': {'NV': n}, 'retrieved': TODAY,
        'snapshot': _arcgis_snapshot_manifest(snapshot),
        'source_inventory': source_inventory,
        'spatial_clip': spatial_clip,
        'catalog_count_reconciliation': {
            'live_service_polygons': stats['source_records'],
            'tiled_nevada_polygons': n,
            'catalog_mapped_polygon_claim': 534,
            'catalog_described_district_claim': 526,
            'status': 'documented_live_service_catalog_drift',
            'note': (
                'The current official service is tiled exactly as returned. '
                'Its count is not silently rewritten to either catalog number.'),
        },
        'required_properties': list(REQUIRED_PROVENANCE),
        'provenance_note': (
            'This index locates district source files; it is not a '
            'ground-truthed mineral-tenure boundary.'),
        **artifact,
    }


def _strict_manifest_bytes():
    with open(MANIFEST, 'rb') as source:
        raw = source.read()
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f'public manifest is invalid JSON: {exc}') from exc
    if not isinstance(value, dict):
        raise RuntimeError('public manifest root must be an object')
    return raw, value


def _publish(pending, entries):
    manifest_raw, manifest = _strict_manifest_bytes()
    manifest_sha = hashlib.sha256(manifest_raw).hexdigest()
    baselines = manifest.setdefault('national_baselines', {})
    if not isinstance(baselines, dict):
        raise RuntimeError('manifest national_baselines must be an object')
    baselines.update(entries)

    directory = os.path.dirname(MANIFEST)
    handle, pending_manifest = tempfile.mkstemp(
        prefix='.manifest-nv-state-survey-', dir=directory)
    backups = {}
    installed = []
    try:
        os.fchmod(handle, stat.S_IMODE(os.stat(MANIFEST).st_mode))
        with os.fdopen(handle, 'w', encoding='utf-8') as output:
            json.dump(manifest, output, separators=(',', ':'), allow_nan=False)
            output.flush()
            os.fsync(output.fileno())
        with open(MANIFEST, 'rb') as current:
            if hashlib.sha256(current.read()).hexdigest() != manifest_sha:
                raise RuntimeError('public manifest changed during Nevada build')
        for key, final_path in BASELINE_KEYS.items():
            source_path = pending[key]
            backup = os.path.join(os.path.dirname(source_path), f'previous-{key}.pmtiles')
            if os.path.exists(final_path):
                os.replace(final_path, backup)
                backups[final_path] = backup
            os.replace(source_path, final_path)
            installed.append(final_path)
        # Catch another builder that stamped after the first race check but
        # before the archive set was installed. Roll back all three archives.
        with open(MANIFEST, 'rb') as current:
            if hashlib.sha256(current.read()).hexdigest() != manifest_sha:
                raise RuntimeError('public manifest changed during Nevada publication')
        os.replace(pending_manifest, MANIFEST)
    except BaseException:
        for final_path in reversed(installed):
            try:
                os.unlink(final_path)
            except FileNotFoundError:
                pass
            backup = backups.get(final_path)
            if backup and os.path.exists(backup):
                os.replace(backup, final_path)
        raise
    finally:
        try:
            os.unlink(pending_manifest)
        except FileNotFoundError:
            pass
        for backup in backups.values():
            try:
                os.unlink(backup)
            except FileNotFoundError:
                pass


def _assert_unique_feature_ids(key, entry, meta):
    layers = ([entry.get('source_layer')] if entry.get('source_layer') else
              entry.get('source_layers'))
    observed_ids = {
        layer: set(meta.get('maxzoom_feature_ids', {}).get(layer, []))
        for layer in layers}
    if key == 'nv_usgs_ds249':
        expected_ids = {
            'nv_ds249_geology': (
                set(range(1, 38_697)) -
                {int(source_id) + 1
                 for source_id in DS249_NULL_GEOMETRY['geology']}),
            'nv_ds249_faults': (
                set(range(1, 54_714)) -
                set(DS249_FAULT_ENCODING_EXCLUSION_CONTRACT['fids'])),
        }
    else:
        contract = ARCGIS_SNAPSHOT_CONTRACTS[key]
        excluded = set(entry.get('spatial_clip', {}).get(
            'fully_outside_object_ids') or [])
        if key == 'nv_nbmg_onegeology_250k':
            excluded.update(ONEGEOLOGY_EMPTY_GEOMETRY)
        expected_ids = {layers[0]: (
            set(range(contract['minimum_object_id'],
                      contract['maximum_object_id'] + 1)) - excluded)}
    if observed_ids != expected_ids:
        details = {
            layer: {
                'expected': len(expected_ids[layer]),
                'observed': len(observed_ids[layer]),
                'missing': sorted(expected_ids[layer] - observed_ids[layer])[:100],
                'extra': sorted(observed_ids[layer] - expected_ids[layer])[:100],
            }
            for layer in layers if observed_ids[layer] != expected_ids[layer]
        }
        raise RuntimeError(
            f'{key} unique maxzoom feature IDs do not reconcile: {details}')
    expected_unique = {layer: len(expected_ids[layer]) for layer in layers}
    declared_unique = (
        {layer: entry.get('by_layer', {}).get(
            'geology' if layer == 'nv_ds249_geology' else 'faults', {}).get('n')
         for layer in layers}
        if key == 'nv_usgs_ds249' else {layers[0]: entry.get('n')})
    if declared_unique != expected_unique:
        raise RuntimeError(
            f'{key} manifest tiled counts do not reconcile: '
            f'declared={declared_unique}, expected={expected_unique}')


def _verify_entry(key, entry, *, pmtiles_header=None):
    expected = BASELINE_KEYS[key]
    expected_rel = os.path.relpath(expected, SITE)
    layers = ([entry.get('source_layer')] if entry.get('source_layer') else
              entry.get('source_layers'))
    if (entry.get('schema_version') != 1 or
            entry.get('status') != 'baseline_not_release' or
            entry.get('state') != 'NV' or entry.get('format') != 'pmtiles' or
            entry.get('file') != expected_rel or not isinstance(layers, list) or
            any(not isinstance(layer, str) or not layer for layer in layers)):
        raise RuntimeError(f'manifest {key} baseline schema is invalid')
    if not os.path.isfile(expected):
        raise RuntimeError(f'{key} PMTiles artifact is missing')
    meta = _validate_pmtiles(
        expected, layers, pmtiles_header=pmtiles_header)
    if entry.get('bytes') != os.path.getsize(expected):
        raise RuntimeError(f'{key} byte count does not match artifact')
    if entry.get('sha256') != _sha256(expected):
        raise RuntimeError(f'{key} SHA-256 does not match artifact')
    if entry.get('bounds') != meta['bounds']:
        raise RuntimeError(f'{key} bounds do not match artifact')
    if entry.get('semantic_tile_feature_counts') != \
            meta['semantic_layer_counts']:
        raise RuntimeError(
            f'{key} semantic tile feature counts do not match artifact')
    _assert_unique_feature_ids(key, entry, meta)
    if entry.get('required_properties') != list(REQUIRED_PROVENANCE):
        raise RuntimeError(f'{key} required provenance schema is invalid')
    if (not isinstance(entry.get('retrieved'), str) or
            re.fullmatch(r'\d{4}-\d{2}-\d{2}', entry['retrieved']) is None):
        raise RuntimeError(f'{key} retrieval date is invalid')
    n = entry.get('n')
    if (not isinstance(n, int) or isinstance(n, bool) or n <= 0 or
            entry.get('states') != {'NV': n}):
        raise RuntimeError(f'{key} source counts are invalid')
    return {'features': n, 'bytes': entry['bytes'], 'sha256': entry['sha256']}


def _validate_manifest_repair(evidence, contract, *, ordering, area_units):
    if not isinstance(evidence, dict):
        raise RuntimeError('topology-repair evidence is missing')
    ids = evidence.get('object_ids')
    expected_method = ('GEOSMakeValid via shapely.make_valid'
                       if contract['count'] else None)
    expected_status = ('reviewed_pinned_source_repair'
                       if contract['count'] else
                       'reviewed_pinned_source_no_repair_required')
    basic = {
        'status': expected_status,
        'ordering': ordering,
        'method': expected_method,
        'shapely_version': contract['shapely_version'],
        'geos_version': contract['geos_version'],
        'count': contract['count'],
        'object_ids_sha256': contract['object_ids_sha256'],
        'validity_reason_counts': contract['validity_reason_counts'],
        'type_transition_counts': contract['type_transition_counts'],
        'nonpolygon_parts_dropped_by_type':
            contract['nonpolygon_parts_dropped_by_type'],
    }
    if any(evidence.get(field) != value for field, value in basic.items()):
        raise RuntimeError('topology-repair identity/engine/type contract is invalid')
    if (not isinstance(ids, list) or len(ids) != contract['count'] or
            len(ids) != len(set(map(str, ids))) or
            _canonical_sha256(ids) != contract['object_ids_sha256'] or
            re.fullmatch(r'[0-9a-f]{64}',
                         str(evidence.get('records_sha256') or '')) is None):
        raise RuntimeError('topology-repair object-ID/hash evidence is invalid')
    area = evidence.get('area_delta')
    absolute = ((area or {}).get('maximum_absolute') or {})
    relative = ((area or {}).get('maximum_relative') or {})
    if ((area or {}).get('units') != area_units or
            absolute.get('acceptance_ceiling') !=
            contract['max_absolute_area_delta'] or
            relative.get('acceptance_ceiling') !=
            contract['max_relative_area_delta']):
        raise RuntimeError('topology-repair area units/ceilings are invalid')
    for metric, ceiling in (
            (absolute, contract['max_absolute_area_delta']),
            (relative, contract['max_relative_area_delta'])):
        value = metric.get('value')
        oid = metric.get('object_id')
        if (not isinstance(value, (int, float)) or isinstance(value, bool) or
                not math.isfinite(value) or not 0 <= value <= ceiling or
                (contract['count'] and str(oid) not in set(map(str, ids))) or
                (not contract['count'] and oid is not None)):
            raise RuntimeError('topology-repair area maximum evidence is invalid')
    total = (area or {}).get('sum_absolute')
    if (not isinstance(total, (int, float)) or isinstance(total, bool) or
            not math.isfinite(total) or total < 0):
        raise RuntimeError('topology-repair summed area evidence is invalid')


def _validate_manifest_encoding_exclusions(evidence):
    contract = DS249_FAULT_ENCODING_EXCLUSION_CONTRACT
    if not isinstance(evidence, dict):
        raise RuntimeError('DS 249 fault encoding-exclusion evidence is missing')
    exact = {
        'status': 'reviewed_below_encoding_scale_exclusion',
        'method': 'no_geometry_fabrication_source_record_omitted',
        'reason_code': contract['reason'],
        'source_scale': '1:250,000', 'native_crs': 'EPSG:26711',
        **{field: contract[field] for field in (
            'tippecanoe_version', 'tippecanoe_maxzoom',
            'tippecanoe_full_detail', 'count', 'fids', 'fids_sha256',
            'source_record_ids', 'source_record_ids_sha256',
            'records_sha256', 'source_geometry_hashes_sha256',
            'minimum_native_length_m', 'maximum_native_length_m',
            'sum_native_length_m', 'maximum_accepted_native_length_m')},
    }
    if any(evidence.get(field) != value for field, value in exact.items()):
        raise RuntimeError('DS 249 fault encoding-exclusion contract is invalid')
    records = evidence.get('records')
    if (not isinstance(records, list) or len(records) != contract['count'] or
            [record.get('fid') for record in records
             if isinstance(record, dict)] != contract['fids'] or
            [record.get('source_record_id') for record in records
             if isinstance(record, dict)] != contract['source_record_ids'] or
            _canonical_sha256(records) != contract['records_sha256'] or
            _canonical_sha256([
                record.get('source_geometry_sha256') for record in records
            ]) != contract['source_geometry_hashes_sha256']):
        raise RuntimeError(
            'DS 249 fault encoding-exclusion record/hash evidence is invalid')
    lengths = []
    for record in records:
        length = record.get('native_length_m')
        if (set(record) != {
                'fid', 'source_record_id', 'source_type', 'coordinate_count',
                'native_length_m', 'source_geometry_sha256'} or
                record.get('source_type') != 'LineString' or
                record.get('coordinate_count') != 2 or
                not isinstance(length, (int, float)) or isinstance(length, bool) or
                not math.isfinite(length) or
                not 0 < length <= contract['maximum_accepted_native_length_m'] or
                re.fullmatch(r'[0-9a-f]{64}', str(
                    record.get('source_geometry_sha256') or '')) is None):
            raise RuntimeError(
                'DS 249 fault encoding-exclusion source geometry is invalid')
        lengths.append(length)
    if (min(lengths) != contract['minimum_native_length_m'] or
            max(lengths) != contract['maximum_native_length_m'] or
            sum(lengths) != contract['sum_native_length_m'] or
            lengths != contract['native_lengths_m'] or
            [record['source_geometry_sha256'] for record in records] !=
            contract['source_geometry_sha256']):
        raise RuntimeError(
            'DS 249 fault encoding-exclusion length evidence is invalid')


def _validate_manifest_clip(key, entry, empty_ids):
    snapshot = entry.get('snapshot')
    contract = ARCGIS_SNAPSHOT_CONTRACTS[key]
    if snapshot != contract:
        raise RuntimeError(f'{key} ArcGIS snapshot contract is invalid')
    inventory = entry.get('source_inventory')
    clip = entry.get('spatial_clip')
    clip_contract = SPATIAL_CLIP_CONTRACTS[key]
    if not isinstance(inventory, dict) or not isinstance(clip, dict):
        raise RuntimeError(f'{key} source/clip evidence is missing')
    outside = clip.get('fully_outside_object_ids')
    clipped = clip.get('geometry_clipped_object_ids')
    if (inventory.get('source_records') != contract['n'] or
            inventory.get('n') != clip_contract['tiled_n'] or
            entry.get('n') != clip_contract['tiled_n'] or
            inventory.get('empty_geometry_count') != len(empty_ids) or
            inventory.get('empty_geometry_object_ids') != empty_ids):
        raise RuntimeError(f'{key} source/empty/tiled counts do not reconcile')
    if (not isinstance(outside, list) or not isinstance(clipped, list) or
            len(outside) != len(set(outside)) or
            len(clipped) != len(set(clipped)) or
            any(not _positive_oid(value, 'clip object ID')
                for value in (*outside, *clipped)) or
            set(outside) & set(clipped) or
            set(empty_ids) & (set(outside) | set(clipped))):
        raise RuntimeError(f'{key} clip object-ID lists are invalid')
    exact_clip = {
        field: clip.get(field) for field in clip_contract
        if field != 'tiled_n'
    }
    if (exact_clip != {field: value for field, value in clip_contract.items()
                      if field != 'tiled_n'} or
            _canonical_sha256(outside) !=
            clip_contract['fully_outside_object_ids_sha256'] or
            _canonical_sha256(clipped) !=
            clip_contract['geometry_clipped_object_ids_sha256'] or
            clip.get('geometry_unchanged_count') !=
            clip_contract['tiled_n'] - len(clipped) or
            contract['n'] !=
            clip_contract['tiled_n'] + len(empty_ids) + len(outside)):
        raise RuntimeError(f'{key} authoritative clip inventory changed')
    pre = clip.get('preclip_area_square_degrees')
    post = clip.get('postclip_area_square_degrees')
    removed = clip.get('area_removed_square_degrees')
    if (clip.get('ordering') != 'topology_repair_before_state_intersection' or
            clip.get('artifact') != os.path.relpath(STATE_CLIPS, ROOT) or
            clip.get('artifact_sha256') != _sha256(STATE_CLIPS) or
            'TIGERweb' not in str(clip.get('authority') or '') or
            clip.get('method') != 'geometric intersection' or
            any(not isinstance(value, (int, float)) or isinstance(value, bool) or
                not math.isfinite(value) for value in (pre, post, removed)) or
            not 0 <= post <= pre or removed < 0 or
            not math.isclose(pre - post, removed, rel_tol=1e-12, abs_tol=1e-12)):
        raise RuntimeError(f'{key} authoritative spatial-clip evidence is invalid')


def validate_manifest_baselines(manifest, *, pmtiles_header=None):
    """Offline exact validation used by both ``--check`` and national CI."""
    baselines = manifest.get('national_baselines')
    if not isinstance(baselines, dict):
        raise RuntimeError('manifest national_baselines is missing')
    result = {}
    for key in BASELINE_KEYS:
        entry = baselines.get(key)
        if not isinstance(entry, dict):
            raise RuntimeError(f'manifest national_baselines.{key} is missing')
        result[key] = _verify_entry(
            key, entry, pmtiles_header=pmtiles_header)
    ds = baselines['nv_usgs_ds249']
    layers = ds.get('by_layer')
    if (not isinstance(layers, dict) or set(layers) != {'geology', 'faults'} or
            ds.get('source', {}).get('bulk_bytes') != DS249_BYTES or
            ds.get('source', {}).get('bulk_sha256') != DS249_SHA256 or
            ds.get('source', {}).get('native_crs') != 'EPSG:26711' or
            ds.get('source', {}).get('source_scale') != '1:250,000' or
            any(layers.get(kind, {}).get(
                    'geometry_inventory') != DS249_GEOMETRY_CONTRACT[kind]
                for kind in ('geology', 'faults'))):
        raise RuntimeError('DS 249 source/null/geometry inventory is invalid')
    for kind in ('geology', 'faults'):
        layer = layers[kind]
        source_geometry_records = sum(
            DS249_GEOMETRY_CONTRACT[kind]['by_type'].values())
        exclusion_count = (
            DS249_FAULT_ENCODING_EXCLUSION_CONTRACT['count']
            if kind == 'faults' else 0)
        expected_n = source_geometry_records - exclusion_count
        expected_source = source_geometry_records + len(
            DS249_NULL_GEOMETRY[kind])
        if (layer.get('source_records') != expected_source or
                layer.get('n') != expected_n or
                layer.get('null_geometry_count') !=
                len(DS249_NULL_GEOMETRY[kind]) or
                layer.get('null_geometry_source_record_ids') !=
                DS249_NULL_GEOMETRY[kind]):
            raise RuntimeError(f'DS 249 {kind} source counts are invalid')
    rendered = sum(layers[kind]['n'] for kind in layers)
    tileable_source = sum(
        sum(DS249_GEOMETRY_CONTRACT[kind]['by_type'].values())
        for kind in layers)
    raw_source = sum(layers[kind]['source_records'] for kind in layers)
    if (ds.get('n') != rendered or ds.get('states') != {'NV': rendered} or
            ds.get('source_records') != tileable_source or
            ds.get('raw_source_records') != raw_source or
            ds.get('null_geometry_count') !=
            sum(len(ids) for ids in DS249_NULL_GEOMETRY.values()) or
            ds.get('encoding_exclusion_count') !=
            DS249_FAULT_ENCODING_EXCLUSION_CONTRACT['count'] or
            layers['geology'].get('encoding_exclusions') is not None):
        raise RuntimeError('DS 249 total count does not reconcile')
    _validate_manifest_encoding_exclusions(
        layers['faults'].get('encoding_exclusions'))
    _validate_manifest_repair(
        layers['geology']['topology_repair'], DS249_GEOLOGY_REPAIR_CONTRACT,
        ordering='validate_then_make_valid_in_epsg26711_then_epsg4326_transform',
        area_units='square meters in EPSG:26711')
    _validate_manifest_repair(
        layers['faults']['topology_repair'], DS249_FAULT_REPAIR_CONTRACT,
        ordering='validate_in_epsg26711_then_epsg4326_transform',
        area_units='square meters in EPSG:26711')
    one = baselines['nv_nbmg_onegeology_250k']
    district = baselines['nv_nbmg_mining_districts']
    _validate_manifest_clip(
        'nv_nbmg_onegeology_250k', one, ONEGEOLOGY_EMPTY_GEOMETRY)
    _validate_manifest_clip('nv_nbmg_mining_districts', district, [])
    _validate_manifest_repair(
        one['source_inventory']['topology_repair'], ONEGEOLOGY_REPAIR_CONTRACT,
        ordering='validate_then_make_valid_then_state_intersection',
        area_units='square degrees in EPSG:4326')
    _validate_manifest_repair(
        district['source_inventory']['topology_repair'],
        DISTRICT_REPAIR_CONTRACT,
        ordering='validate_then_make_valid_then_state_intersection',
        area_units='square degrees in EPSG:4326')
    reconciliation = district.get('catalog_count_reconciliation')
    if (not isinstance(reconciliation, dict) or
            reconciliation.get('live_service_polygons') !=
            district.get('snapshot', {}).get('n') or
            reconciliation.get('tiled_nevada_polygons') != district['n'] or
            reconciliation.get('status') != 'documented_live_service_catalog_drift'):
        raise RuntimeError('NBMG district live/catalog count reconciliation is invalid')
    return result


def check():
    _, manifest = _strict_manifest_bytes()
    result = validate_manifest_baselines(manifest)
    print(json.dumps(result, indent=2))
    return result


def build(grace_seconds=0):
    if not shutil.which('tippecanoe'):
        raise RuntimeError('tippecanoe >=2.79 with PMTiles output is required')
    observed_tippecanoe = _tippecanoe_version()
    if observed_tippecanoe != \
            DS249_FAULT_ENCODING_EXCLUSION_CONTRACT['tippecanoe_version']:
        raise RuntimeError(
            'tippecanoe version changed; re-audit the Nevada encoding '
            f'exclusions before building: {observed_tippecanoe}')
    if fiona is None or transform_geom is None:
        raise RuntimeError(
            'Fiona is required; run with a geospatial Python environment '
            '(for this workspace: /Users/matthewlew/miniconda3/bin/python)')
    if any(value is None for value in (
            shapely, shapely_make_valid, shapely_mapping, shapely_shape,
            shapely_unary_union, shapely_prepare, shapely_explain_validity)):
        raise RuntimeError('Shapely 2.x is required for authoritative state clipping')
    if not 0 <= grace_seconds <= 60:
        raise RuntimeError('manifest grace must be from 0 to 60 seconds')
    # Load the canonical semantic PMTiles validator before any network work.
    from validate_national import _pmtiles_header  # noqa: F401

    os.makedirs(OUT_DIR, exist_ok=True)
    # Statewide source ZIPs, shapefiles, and GeoJSON sequences must never sit
    # below site/, even under a dot-directory: a deploy sync can expose hidden
    # build files. build-inputs is private and on the repository filesystem, so
    # final PMTiles installation still uses same-filesystem atomic renames.
    _ensure_private_staging_root()
    with tempfile.TemporaryDirectory(
            prefix='nwmm-nv-baselines-', dir=PRIVATE_STAGING_ROOT) as temp:
        nv_clip = _load_nv_clip()
        ds_zip = os.path.join(temp, '249.zip')
        download = _download_ds249(ds_zip)
        source_paths = _extract_ds249(ds_zip, temp)
        ds_geology_seq = os.path.join(temp, 'ds249-geology.geojsonseq')
        ds_faults_seq = os.path.join(temp, 'ds249-faults.geojsonseq')
        ds_stats = _stream_ds249(
            source_paths, ds_geology_seq, ds_faults_seq)

        one_snapshot = _layer_snapshot(
            NBMG_GEOLOGY, 'US-NV_NBMG_250k_Geology',
            'esriGeometryPolygon', '1:250,000')
        _assert_arcgis_snapshot_contract(
            'nv_nbmg_onegeology_250k', one_snapshot)
        one_seq = os.path.join(temp, 'nbmg-onegeology.geojsonseq')
        one_stats = _stream_arcgis(
            NBMG_GEOLOGY, one_snapshot,
            ('OBJECTID', 'County', 'identifier', 'name', 'description',
             'geologicUnitType', 'lithology', 'geologicHistory',
             'observationMethod', 'positionalAccuracy', 'source',
             'genericSymbolizer'),
            _normalize_onegeology, one_seq,
            clip=nv_clip,
            expected_empty_geometry_ids=ONEGEOLOGY_EMPTY_GEOMETRY,
            repair_contract=ONEGEOLOGY_REPAIR_CONTRACT)

        district_snapshot = _layer_snapshot(
            NBMG_DISTRICTS, 'Mining Districts',
            'esriGeometryPolygon', '1:1,000,000',
            scale_in_layer_metadata=False)
        _assert_arcgis_snapshot_contract(
            'nv_nbmg_mining_districts', district_snapshot)
        district_seq = os.path.join(temp, 'nbmg-districts.geojsonseq')
        district_stats = _stream_arcgis(
            NBMG_DISTRICTS, district_snapshot,
            ('OBJECTID_1', 'District_Name', 'District_Type', 'District_No'),
            _normalize_district, district_seq, clip=nv_clip,
            repair_contract=DISTRICT_REPAIR_CONTRACT)
        if (one_stats['source_records'] != len(one_snapshot['ids']) or
                district_stats['source_records'] != len(district_snapshot['ids'])):
            raise RuntimeError('ArcGIS snapshot counts do not reconcile')

        pending = {
            'nv_usgs_ds249': os.path.join(temp, 'usgs-ds249.pmtiles'),
            'nv_nbmg_onegeology_250k': os.path.join(
                temp, 'nbmg-onegeology-250k.pmtiles'),
            'nv_nbmg_mining_districts': os.path.join(
                temp, 'nbmg-report47-districts.pmtiles'),
        }
        _run_tippecanoe(
            pending['nv_usgs_ds249'],
            (('nv_ds249_geology', ds_geology_seq),
             ('nv_ds249_faults', ds_faults_seq)),
            'U.S. Geological Survey DS 249; prepared with NBMG', 12)
        _run_tippecanoe(
            pending['nv_nbmg_onegeology_250k'],
            (('nv_nbmg_onegeology_250k', one_seq),),
            'Nevada Bureau of Mines and Geology OneGeology', 12)
        _run_tippecanoe(
            pending['nv_nbmg_mining_districts'],
            (('nv_nbmg_mining_districts', district_seq),),
            'Nevada Bureau of Mines and Geology Report 47z', 10)

        ds_meta = _validate_pmtiles(
            pending['nv_usgs_ds249'],
            ('nv_ds249_geology', 'nv_ds249_faults'))
        one_meta = _validate_pmtiles(
            pending['nv_nbmg_onegeology_250k'],
            ('nv_nbmg_onegeology_250k',))
        district_meta = _validate_pmtiles(
            pending['nv_nbmg_mining_districts'],
            ('nv_nbmg_mining_districts',))
        entries = {
            'nv_usgs_ds249': _ds249_entry(
                ds_stats, download,
                _artifact_fields(pending['nv_usgs_ds249'], ds_meta)),
            'nv_nbmg_onegeology_250k': _onegeology_entry(
                one_snapshot, one_stats, nv_clip['manifest'],
                _artifact_fields(pending['nv_nbmg_onegeology_250k'], one_meta)),
            'nv_nbmg_mining_districts': _district_entry(
                district_snapshot, district_stats, nv_clip['manifest'],
                _artifact_fields(pending['nv_nbmg_mining_districts'], district_meta)),
        }
        for key, meta in (
                ('nv_usgs_ds249', ds_meta),
                ('nv_nbmg_onegeology_250k', one_meta),
                ('nv_nbmg_mining_districts', district_meta)):
            _assert_unique_feature_ids(key, entries[key], meta)
        print(f'Nevada archives validated; manifest stamp begins in '
              f'{grace_seconds} seconds')
        if grace_seconds:
            time.sleep(grace_seconds)
        _publish(pending, entries)
    result = {
        key: {
            'artifact': os.path.relpath(path, SITE),
            'features': entries[key]['n'], 'bytes': entries[key]['bytes'],
            'sha256': entries[key]['sha256'],
        }
        for key, path in BASELINE_KEYS.items()
    }
    print(json.dumps(result, indent=2))
    return result


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--check', action='store_true',
                        help='validate published artifacts without network access')
    parser.add_argument('--manifest-grace-seconds', type=int, default=0,
                        help='bounded coordination window before the manifest stamp')
    args = parser.parse_args(argv)
    if args.check:
        check()
    else:
        build(args.manifest_grace_seconds)


if __name__ == '__main__':
    main()
