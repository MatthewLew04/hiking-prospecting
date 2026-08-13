#!/usr/bin/env python3
"""Build exact Arizona state-survey PMTiles without releasing Arizona.

The official Arizona Geological Survey (AZGS) 2025 republication of Map 35
provides a GeMS-compatible GeoPackage.  Its generated collection ZIP is a
transport wrapper whose timestamps and wrapper hash change per request, so
this builder pins the extracted ``AZStatewide.gpkg`` member and the canonical
AZGS catalog metadata instead of pretending that the wrapper is immutable.

Two small official University of Arizona/AZGS ArcGIS layers provide mining
district polygons and compiled critical-mineral occurrences.  They are
snapshotted by object ID, fetched by exact POST object-ID pages, content-hashed
twice, and checked again after the second pass.

All source intermediates remain below private ``build-inputs/.staging``.  A
publication installs three PMTiles archives plus their manifest entries as one
rollback-safe transaction.  This is baseline-only work: it never changes an
Arizona release flag or DONE-gate state.  The default CLI action is a private
audit; publication requires an explicit ``--publish``.
"""
from __future__ import annotations

import argparse
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
except ImportError:  # pragma: no cover - exercised by build preflight
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
except ImportError:  # pragma: no cover - exercised by build preflight
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
OUT_DIR = os.path.join(SITE, 'data', 'tiles', 'states', 'az')
PRIVATE_STAGING_ROOT = os.path.join(ROOT, 'build-inputs', '.staging')
STATE_CLIPS = os.path.join(ROOT, 'infra', 'state_clips.json')

MAP35_OUT = os.path.join(OUT_DIR, 'azgs-map35.pmtiles')
DISTRICTS_OUT = os.path.join(OUT_DIR, 'azgs-mining-districts.pmtiles')
OCCURRENCES_OUT = os.path.join(OUT_DIR, 'azgs-critical-minerals.pmtiles')
BASELINE_KEYS = {
    'az_azgs_map35_2025': MAP35_OUT,
    'az_azgs_mining_districts': DISTRICTS_OUT,
    'az_azgs_critical_minerals': OCCURRENCES_OUT,
}
ARCHIVE_LABELS = {
    'az_azgs_map35_2025':
        'Arizona Geological Survey Map 35 (2025 GeMS conversion)',
    'az_azgs_mining_districts':
        'Arizona Geological Survey Mining Districts',
    'az_azgs_critical_minerals':
        'Arizona Geological Survey compiled critical-mineral occurrences',
}

MAP35_COLLECTION_ID = 'AGMS-1749135591815-872'
MAP35_COLLECTION_URL = (
    'https://data.azgs.arizona.edu/api/v1/collections/' + MAP35_COLLECTION_ID)
MAP35_METADATA_URL = (
    'https://data.azgs.arizona.edu/api/v1/metadata?collection_id=' +
    MAP35_COLLECTION_ID)
MAP35_CATALOG_URL = (
    'https://library.azgs.arizona.edu/item/' + MAP35_COLLECTION_ID)
MAP35_MEMBER = (
    f'{MAP35_COLLECTION_ID}/gisdata/layers/AZStatewide.gpkg')
MAP35_MEMBER_BYTES = 28_155_904
MAP35_MEMBER_SHA256 = (
    'ee871b3fa38ec32e1fe4b41608b94758094b76ea4f2db7ef5a54c584c108924e')
MAP35_CATALOG_METADATA_SHA256 = (
    'acd549849cf3f7b924b102d26148dbf819b61dd2f4b1138170e41e0a5f9b1d10')
MAP35_WRAPPER_MEMBERS = {
    f'{MAP35_COLLECTION_ID}/',
    f'{MAP35_COLLECTION_ID}/azgs.json',
    f'{MAP35_COLLECTION_ID}/gisdata/',
    f'{MAP35_COLLECTION_ID}/gisdata/gems2/',
    f'{MAP35_COLLECTION_ID}/gisdata/layers/',
    f'{MAP35_COLLECTION_ID}/gisdata/gems2/AZStatewide.gdb.zip',
    MAP35_MEMBER,
    f'{MAP35_COLLECTION_ID}/gisdata/layers/Statewide.kmz',
}

MAP35_LAYER_CONTRACTS = {
    'ContactsAndFaults': {
        'n': 15_563, 'crs': 'EPSG:26912',
        'declared_geometry': 'MultiLineString',
        'schema_sha256':
            '7949e26bcae6d2b5ac9c8539b0ed39ca6c5ff4b88fc0e980ab16b69c23909b4d',
        'fid_sha256':
            '613ff79d7fd6fdcabe8a886218186e1dcf9ca6df84352ec3eaed03f27152cb47',
        'source_ids_sha256':
            'ebad26925741db653a2f87d63571447445961762392529b82c07b5262a1f45b4',
        'source_id_field': 'ContactsAndFaults_ID',
        'geometry_types': {'MultiLineString': 15_563},
        'fully_outside_count': 2,
        'fully_outside_fids_sha256':
            'bb76b159f540f6298c9c68ab1ada5825a558b1d94abc66ad3f5983a7f2a39ed5',
        'geometry_clipped_count': 109,
        'geometry_clipped_fids_sha256':
            '0f91f77bdae15c40d64d3ca88be65c53290e3e244ebc33afef09bb46321d290f',
    },
    'MapUnitPolys': {
        'n': 4_841, 'crs': 'EPSG:26912',
        'declared_geometry': 'MultiPolygon',
        'schema_sha256':
            '42fec93f873d3abf2131476f3a85539c3e9526b41cd308277811e3127c71ddf5',
        'fid_sha256':
            '5bbc7fc62f3b4ddb91bfeb0034c253fcc387aec624c7db4bfd000bb8be919227',
        'source_ids_sha256':
            '258b12de56876d3db1a09120cda4e7c558beaeee3be04faef64f9b2dab4459f1',
        'source_id_field': 'MapUnitPolys_ID',
        'geometry_types': {'MultiPolygon': 4_841},
        'fully_outside_count': 0,
        'fully_outside_fids_sha256':
            '4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945',
        'geometry_clipped_count': 118,
        'geometry_clipped_fids_sha256':
            '4af2a8b08a97b386bf783c6f8830fa0492eb752da25d3488c9434ccd050ae0e2',
    },
}

MAP35_TABLE_CONTRACTS = {
    'DataSources': (189,
        '654504c943886e5d4cc944b97195266d553e70c34cc3e8b10b974df35cb26012'),
    'DescriptionOfMapUnits': (50,
        'b2f9a37a0796b332db0da5485af0c9a62b91c490d8fdae03f251d268f5d99886'),
    'Glossary': (6,
        '8d47c8a6d10c995c34b147f64ade1095708bfc28e65ded43e89043b1fb15656f'),
    'Symbology': (2_273,
        '53239707c5c6345c8175771b786c5f057ad03b12e3fef251aa54385fa388c817'),
}

# These two valid, two-vertex traces are 0.219 m and 1.597 m long in a
# 1:1,000,000 compilation.  They collapse at z12 MVT quantization.  Their exact
# source geometry is inventoried; no longer geometry is fabricated.
MAP35_FAULT_ENCODING_EXCLUSIONS = {
    'fids': [11_371, 11_825],
    'fids_sha256':
        'e7e012a6cdf8299ee89911b75040b0b545193b5fd59b6aecfced6461754a7995',
    'records_sha256':
        '5ee3500e84382c31f69fc16683deb00435cfd06b6afcd551da922751c5edfd89',
    'records': [
        {
            'fid': 11_371,
            'source_record_id': 'GMA.ContactsAndFaults.5333',
            'native_length_m': 0.21854283375675693,
            'source_type': 'MultiLineString', 'coordinate_count': 2,
            'source_geometry_sha256':
                '7f354dadef0b959a8b0f8b76f481a86c289eb2b1eeee40a3b853c814220c9c90',
        },
        {
            'fid': 11_825,
            'source_record_id': 'GMA.ContactsAndFaults.5872',
            'native_length_m': 1.5968714703656155,
            'source_type': 'MultiLineString', 'coordinate_count': 2,
            'source_geometry_sha256':
                'b9ab56dbd7c1fc1abe7406b2dc1ea41cd989dbe136ba13861458bdbf39b09aed',
        },
    ],
    'reason': 'below_mvt_maxzoom_encoding_resolution',
    'tippecanoe_maxzoom': 12, 'tippecanoe_full_detail': 12,
}

AZGS_FEATURE_SERVICE = (
    'https://services1.arcgis.com/Ezk9fcjSUkeadg6u/arcgis/rest/services/'
    'AZGS_Mining_Districts_WFL1/FeatureServer')
AZGS_SERVICE_ITEM_ID = 'beef607714624113b8f69c2a4bbc6a2d'
AZGS_SERVICE_ITEM_URL = (
    'https://www.arcgis.com/sharing/rest/content/items/' +
    AZGS_SERVICE_ITEM_ID)
AZGS_SERVICE_ITEM_METADATA_SHA256 = (
    'a03846a2b182c9f6b1fb0ca6a948c7e8fb277b36fa1807109480e7220553f282')

ARCGIS_LAYERS = {
    'az_azgs_mining_districts': {
        'url': AZGS_FEATURE_SERVICE + '/1', 'layer_id': 1,
        'name': 'AZGS Mining Districts',
        'geometry_type': 'esriGeometryPolygon', 'oid_field': 'FID',
        'fields': ('FID', 'Id', 'County', 'D_Name', 'Other_Name',
                   'Reference', 'Comms', 'Phys'),
        'n': 287, 'minimum_object_id': 1, 'maximum_object_id': 287,
        'object_ids_sha256':
            'e93726bb1413065fae7728d48ad3995f732da3f0f44ad43c140382ffed719baa',
        'layer_metadata_sha256':
            'b5043842c6984e937c76b2877adda0ef48a14190e77e89d8afe4ad265a62df90',
        'source_content_sha256':
            'db9ec78a9fa01e4913aa434411414123f59d5fad25f37abfc9bfd3506c7b451d',
        'fully_outside_count': 0,
        'fully_outside_object_ids_sha256':
            '4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945',
        'geometry_clipped_count': 12,
        'geometry_clipped_object_ids_sha256':
            'f4be75a7541f1ae0408c79ecbfddfac9e66bffb9e48e6c1cb71cdbdbc1b5ad28',
        'source_layer': 'az_azgs_mining_districts',
    },
    'az_azgs_critical_minerals': {
        'url': AZGS_FEATURE_SERVICE + '/0', 'layer_id': 0,
        'name': 'Compiled AZ Critical Mineral Resource Deposits / Occurrences',
        'geometry_type': 'esriGeometryPoint', 'oid_field': 'OBJECTID',
        # Public contact fields are deliberately not republished.
        'fields': (
            'OBJECTID', 'State', 'Commodity', 'Site_Name', 'GPSX', 'GPSY',
            'Resource', 'Res_AppxCont', 'Res_AppxContUt', 'Ref_Link',
            'Citation', 'Notes', 'Host_Rocks_Age___Lithology',
            'Igneous_rocks', 'High_temperature_alteration',
            'Structural_geology', 'Mineralization_Age',
            'Low_temperature_alteration', 'Gangue_and_ore_mineralogy',
            'Deposit_Type', 'Old_District_Name', 'New_District_Name'),
        'n': 24, 'minimum_object_id': 1, 'maximum_object_id': 24,
        'object_ids_sha256':
            '8f90d60eb2ced2ac872ae92dd2f6f1d4ee8c3c1254232268d2a2130cc546dc50',
        'layer_metadata_sha256':
            '1a61dbc50c07490c568edc382e9b36ddca0602524e3214b0edb6c8f56c0529a2',
        'source_content_sha256':
            'a8a59c1fd64030575435f7d43241d2769c9c34bc57db5761560cad997ce03db5',
        'fully_outside_count': 0,
        'fully_outside_object_ids_sha256':
            '4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945',
        'geometry_clipped_count': 0,
        'geometry_clipped_object_ids_sha256':
            '4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945',
        'source_layer': 'az_azgs_critical_minerals',
    },
}

AZ_BOUNDS = (-114.82, 31.29, -109.02, 37.02)
REQUIRED_PROVENANCE = (
    'fid', 'st', 'source_dataset', 'source_id', 'source_record_id',
    'source_scale', 'source_scale_status', 'source_ref', 'source_url',
    'publication_id')
MAP35_LAYERS = ('az_azgs_map35_geology', 'az_azgs_map35_faults')
TIPPECANOE_VERSION = 'v2.79.0'
USER_AGENT = (
    'nw-mineral-monitor/11 Arizona state-survey PMTiles builder '
    '(official public research data)')

NO_REPAIR_CONTRACT = {
    'count': 0,
    'object_ids_sha256':
        '4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945',
    'validity_reason_counts': {}, 'type_transition_counts': {},
    'nonpolygon_parts_dropped_by_type': {},
    'shapely_version': '2.0.3', 'geos_version': '3.11.3',
}


def _canonical_sha256(value):
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(',', ':'), allow_nan=False,
    ).encode()).hexdigest()


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _text(value, limit=500):
    if value is None:
        return None
    value = re.sub(r'\s+', ' ', str(value)).strip()
    return value[:limit] if value else None


def _positive_integer(value, label='feature ID'):
    if isinstance(value, bool):
        raise RuntimeError(f'{label} must be a positive integer')
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f'{label} must be a positive integer') from exc
    if parsed <= 0 or str(value).strip() not in (str(parsed), f'{parsed}.0'):
        raise RuntimeError(f'{label} must be a positive integer')
    return parsed


def _ensure_private_staging_root():
    site = os.path.realpath(SITE)
    staging = os.path.realpath(PRIVATE_STAGING_ROOT)
    try:
        inside = os.path.commonpath((site, staging)) == site
    except ValueError as exc:
        raise RuntimeError('Arizona staging path is not resolvable') from exc
    if inside:
        raise RuntimeError('Arizona staging root must be outside public site/')
    os.makedirs(PRIVATE_STAGING_ROOT, exist_ok=True)


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
            last = RuntimeError(f'remote JSON error from {url}: {error}')
            code = error.get('code') if isinstance(error, dict) else None
            if code not in (429, 500, 502, 503, 504):
                raise last
        except urllib.error.HTTPError as exc:
            last = exc
            if exc.code not in (429, 500, 502, 503, 504):
                raise RuntimeError(
                    f'remote JSON failed: HTTP {exc.code} ({url})') from exc
        except (urllib.error.URLError, TimeoutError,
                json.JSONDecodeError) as exc:
            last = exc
        if attempt + 1 < tries:
            time.sleep(min(30, 2 ** attempt))
    raise RuntimeError(f'remote JSON failed after {tries} attempts: {last}')


def _catalog_selection(value):
    rows = value.get('data') if isinstance(value, dict) else None
    if not isinstance(rows, list) or len(rows) != 1:
        raise RuntimeError('AZGS catalog did not return one Map 35 record')
    row = rows[0]
    metadata = row.get('metadata') if isinstance(row, dict) else None
    if not isinstance(metadata, dict):
        raise RuntimeError('AZGS catalog metadata is missing')
    selected = {
        'collection_id': row.get('collection_id'),
        **{key: metadata.get(key) for key in (
            'year', 'files', 'title', 'series', 'authors', 'license',
            'private', 'abstract', 'identifiers', 'bounding_box',
            'informal_name', 'collection_group')},
        'links': row.get('links'),
    }
    if (selected['collection_id'] != MAP35_COLLECTION_ID or
            selected['series'] != 'Map-35' or selected['year'] != '2025' or
            selected['private'] is not False or
            {'name': 'AZStatewide.gpkg', 'type': 'gisdata:layers'} not in
            (selected.get('files') or [])):
        raise RuntimeError('AZGS catalog identity contract changed')
    return selected


def _catalog_snapshot():
    selected = _catalog_selection(_request_json(MAP35_METADATA_URL))
    digest = _canonical_sha256(selected)
    if digest != MAP35_CATALOG_METADATA_SHA256:
        raise RuntimeError(
            'AZGS Map 35 canonical catalog metadata changed; review required: '
            f'{digest}')
    return {'metadata_sha256': digest, 'metadata': selected}


def _download_collection(path):
    request = urllib.request.Request(
        MAP35_COLLECTION_URL, headers={'User-Agent': USER_AGENT})
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
        raise RuntimeError(f'AZGS collection download failed: {exc}') from exc
    if not 20_000_000 <= size <= 25_000_000:
        raise RuntimeError(f'AZGS collection wrapper size is implausible: {size}')
    return {
        'bytes': size, 'sha256': digest.hexdigest(),
        'identity_status': 'dynamic_transport_wrapper_not_source_identity',
    }


def _extract_pinned_geopackage(archive_path, target_path):
    try:
        with zipfile.ZipFile(archive_path) as archive:
            infos = archive.infolist()
            names = [item.filename for item in infos]
            if len(names) != len(set(names)) or set(names) != MAP35_WRAPPER_MEMBERS:
                raise RuntimeError('AZGS collection member inventory changed')
            for item in infos:
                normalized = os.path.normpath(item.filename)
                if (normalized.startswith('..') or os.path.isabs(normalized) or
                        item.flag_bits & 0x1 or
                        stat.S_ISLNK((item.external_attr >> 16) & 0xFFFF)):
                    raise RuntimeError('AZGS collection has an unsafe member')
            member = archive.getinfo(MAP35_MEMBER)
            if member.file_size != MAP35_MEMBER_BYTES:
                raise RuntimeError('AZGS GeoPackage member byte count changed')
            digest = hashlib.sha256()
            size = 0
            with archive.open(member) as source, open(target_path, 'wb') as output:
                for block in iter(lambda: source.read(1024 * 1024), b''):
                    output.write(block)
                    digest.update(block)
                    size += len(block)
    except (OSError, zipfile.BadZipFile, KeyError) as exc:
        raise RuntimeError(f'AZGS collection extraction failed: {exc}') from exc
    observed = digest.hexdigest()
    if size != MAP35_MEMBER_BYTES or observed != MAP35_MEMBER_SHA256:
        raise RuntimeError(
            'AZGS extracted GeoPackage identity changed: '
            f'{size} bytes/{observed}')
    return {'bytes': size, 'sha256': observed, 'member': MAP35_MEMBER}


def _layer_schema(source, layer):
    crs = source.crs.to_string() if source.crs else None
    selected = {
        'layer': layer, 'driver': source.driver, 'crs': crs,
        'geometry': source.schema.get('geometry'),
        'properties': list(source.schema.get('properties', {}).items()),
        'n': len(source),
    }
    return selected, _canonical_sha256(selected)


def _validate_geopackage_schema(path):
    layers = fiona.listlayers(path)
    expected = list(MAP35_LAYER_CONTRACTS) + list(MAP35_TABLE_CONTRACTS)
    if layers != expected:
        raise RuntimeError(
            f'AZGS GeoPackage layer order/inventory changed: {layers}')
    result = {}
    for layer in layers:
        with fiona.open(path, layer=layer) as source:
            selected, digest = _layer_schema(source, layer)
        if layer in MAP35_LAYER_CONTRACTS:
            contract = MAP35_LAYER_CONTRACTS[layer]
            if (selected['n'] != contract['n'] or
                    selected['crs'] != contract['crs'] or
                    selected['geometry'] != contract['declared_geometry'] or
                    digest != contract['schema_sha256']):
                raise RuntimeError(f'AZGS {layer} typed schema changed')
        else:
            n, expected_digest = MAP35_TABLE_CONTRACTS[layer]
            if selected['n'] != n or digest != expected_digest:
                raise RuntimeError(f'AZGS {layer} typed schema changed')
        result[layer] = {'n': selected['n'], 'schema_sha256': digest,
                         'schema': selected}
    return result


def _load_lookups(path):
    with fiona.open(path, layer='DataSources') as source:
        data_sources = {row['properties']['DataSources_ID']:
                        dict(row['properties']) for row in source}
    if (len(data_sources) != MAP35_TABLE_CONTRACTS['DataSources'][0] or
            any(not key or row.get('Source') != key
                for key, row in data_sources.items())):
        raise RuntimeError('AZGS DataSources linkage table is not one-to-one')
    with fiona.open(path, layer='DescriptionOfMapUnits') as source:
        descriptions = {}
        for row in source:
            props = dict(row['properties'])
            unit = props.get('MapUnit')
            if not unit or unit in descriptions:
                raise RuntimeError('AZGS DescriptionOfMapUnits key is invalid')
            descriptions[unit] = props
    if len(descriptions) != MAP35_TABLE_CONTRACTS['DescriptionOfMapUnits'][0]:
        raise RuntimeError('AZGS map-unit description count changed')
    return data_sources, descriptions


def _load_az_clip():
    if any(value is None for value in (
            shapely, shapely_make_valid, shapely_mapping, shapely_shape,
            shapely_unary_union, shapely_prepare, shapely_explain_validity,
            transform_geom)):
        raise RuntimeError('Shapely 2.x and Fiona transforms are required')
    try:
        with open(STATE_CLIPS, 'rb') as source:
            raw = source.read()
        value = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f'authoritative state clip is unreadable: {exc}') from exc
    source_name = value.get('source') if isinstance(value, dict) else None
    states = value.get('states') if isinstance(value, dict) else None
    if (value.get('schema_version') != 1 or
            not isinstance(source_name, str) or 'TIGERweb' not in source_name or
            'January 1 2025' not in source_name or
            not isinstance(states, dict) or set(states) != set(ALL_STATES)):
        raise RuntimeError('state clip must be the exact Census 2025 49-state index')
    geometry = states.get('AZ')
    wgs84 = shapely_shape(geometry)
    native = shapely_shape(transform_geom(
        'EPSG:4326', 'EPSG:26912', geometry, precision=-1))
    if any(item.is_empty or not item.is_valid for item in (wgs84, native)):
        raise RuntimeError('authoritative Arizona clip is empty or invalid')
    return {
        'wgs84': wgs84, 'wgs84_prepared': shapely_prepare(wgs84),
        'native': native, 'native_prepared': shapely_prepare(native),
        'manifest': {
            'authority': source_name, 'method': 'geometric intersection',
            'artifact': os.path.relpath(STATE_CLIPS, ROOT),
            'artifact_sha256': hashlib.sha256(raw).hexdigest(),
        },
    }


def _atomic_parts(geometry):
    if hasattr(geometry, 'geoms'):
        for child in geometry.geoms:
            yield from _atomic_parts(child)
    else:
        yield geometry


def _clip_geometry(geometry, clip_boundary, clip_prepared, atomic_types,
                   metric):
    if geometry.is_empty:
        return None, {'changed': False, 'outside': True,
                      'preclip_metric': 0.0, 'postclip_metric': 0.0}
    if not geometry.is_valid:
        raise RuntimeError(
            f'unreviewed invalid source geometry: '
            f'{shapely_explain_validity(geometry)}')
    before = float(getattr(geometry, metric))
    changed = not clip_prepared.covers(geometry)
    result = geometry.intersection(clip_boundary) if changed else geometry
    parts = [part for part in _atomic_parts(result)
             if part.geom_type in atomic_types and not part.is_empty and
             getattr(part, metric) > 0]
    if not parts:
        return None, {'changed': changed, 'outside': True,
                      'preclip_metric': before, 'postclip_metric': 0.0}
    if changed:
        result = parts[0] if len(parts) == 1 else shapely_unary_union(parts)
    if result.is_empty or not result.is_valid:
        raise RuntimeError('authoritative clip produced invalid geometry')
    return result, {'changed': changed, 'outside': False,
                    'preclip_metric': before,
                    'postclip_metric': float(getattr(result, metric))}


def _positions(value):
    if (isinstance(value, (list, tuple)) and len(value) >= 2 and
            all(isinstance(item, (int, float)) and not isinstance(item, bool)
                for item in value[:2])):
        yield float(value[0]), float(value[1])
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from _positions(child)


def _plain_wgs84_geometry(value, expected):
    if hasattr(value, '__geo_interface__'):
        value = value.__geo_interface__
    value = dict(value)
    geometry_type = value.get('type')
    if geometry_type not in expected:
        raise RuntimeError(
            f'geometry {geometry_type!r} is not one of {sorted(expected)}')
    coordinates = value.get('coordinates')
    positions = list(_positions(coordinates))
    minimum = 1 if geometry_type == 'Point' else (
        4 if geometry_type in ('Polygon', 'MultiPolygon') else 2)
    if len(positions) < minimum:
        raise RuntimeError('geometry has too few coordinate positions')
    for longitude, latitude in positions:
        if (not math.isfinite(longitude) or not math.isfinite(latitude) or
                not AZ_BOUNDS[0] <= longitude <= AZ_BOUNDS[2] or
                not AZ_BOUNDS[1] <= latitude <= AZ_BOUNDS[3]):
            raise RuntimeError(
                f'Arizona feature has out-of-scope coordinate '
                f'({longitude}, {latitude})')
    def convert(item):
        if (isinstance(item, (list, tuple)) and len(item) >= 2 and
                all(isinstance(part, (int, float)) and not isinstance(part, bool)
                    for part in item[:2])):
            return [round(float(item[0]), 8), round(float(item[1]), 8)]
        if not isinstance(item, (list, tuple)) or not item:
            raise RuntimeError('geometry has malformed coordinate nesting')
        return [convert(child) for child in item]

    return {'type': geometry_type, 'coordinates': convert(coordinates)}


def _write_feature(output, feature):
    output.write(json.dumps(feature, separators=(',', ':'), allow_nan=False))
    output.write('\n')


def _base_properties(fid, dataset, source_id, source_record_id, scale,
                     scale_status, source_ref, source_url, publication_id):
    return {
        'fid': fid, 'st': 'AZ', 'source_dataset': dataset,
        'source_id': source_id, 'source_record_id': source_record_id,
        'source_scale': scale, 'source_scale_status': scale_status,
        'source_ref': source_ref, 'source_url': source_url,
        'publication_id': publication_id,
    }


def _geometry_sha256(geometry):
    return _canonical_sha256(json.loads(json.dumps(shapely_mapping(geometry))))


def _line_coordinate_count(geometry):
    return sum(len(part.coords) for part in _atomic_parts(geometry)
               if part.geom_type == 'LineString')


def _encoding_exclusion_evidence(records):
    records = sorted(records, key=lambda row: row['fid'])
    contract = MAP35_FAULT_ENCODING_EXCLUSIONS
    if ([row['fid'] for row in records] != contract['fids'] or
            _canonical_sha256([row['fid'] for row in records]) !=
            contract['fids_sha256'] or
            _canonical_sha256(records) != contract['records_sha256'] or
            records != contract['records']):
        raise RuntimeError('Map 35 fault encoding-exclusion inventory changed')
    return {
        'status': 'reviewed_below_encoding_scale_exclusion',
        'method': 'no_geometry_fabrication_source_record_omitted',
        'reason_code': contract['reason'], 'source_scale': '1:1,000,000',
        'native_crs': 'EPSG:26912', 'count': len(records),
        'fids': contract['fids'], 'fids_sha256': contract['fids_sha256'],
        'records': records, 'records_sha256': contract['records_sha256'],
        'tippecanoe_maxzoom': contract['tippecanoe_maxzoom'],
        'tippecanoe_full_detail': contract['tippecanoe_full_detail'],
    }


def _no_repair_evidence(
        ordering='validate_in_native_crs_then_state_intersection'):
    contract = NO_REPAIR_CONTRACT
    if (shapely.__version__ != contract['shapely_version'] or
            shapely.geos_version_string != contract['geos_version']):
        raise RuntimeError(
            'Shapely/GEOS version changed; re-audit Arizona geometry')
    return {
        'status': 'reviewed_pinned_source_no_repair_required',
        'ordering': ordering,
        'method': None, 'shapely_version': shapely.__version__,
        'geos_version': shapely.geos_version_string, 'count': 0,
        'object_ids': [],
        'object_ids_sha256': contract['object_ids_sha256'],
        'validity_reason_counts': {}, 'type_transition_counts': {},
        'nonpolygon_parts_dropped_by_type': {},
    }


def _clip_evidence(fids, clipped, outside, pre, post, metric, *,
                   ordering='validate_in_native_crs_then_state_intersection'):
    return {
        'ordering': ordering,
        'source_records': len(fids),
        'fully_outside_count': len(outside),
        'fully_outside_object_ids': outside,
        'fully_outside_object_ids_sha256': _canonical_sha256(outside),
        'geometry_clipped_count': len(clipped),
        'geometry_clipped_object_ids': clipped,
        'geometry_clipped_object_ids_sha256': _canonical_sha256(clipped),
        'geometry_unchanged_count': len(fids) - len(outside) - len(clipped),
        f'preclip_{metric}': pre, f'postclip_{metric}': post,
        f'{metric}_removed': pre - post,
    }


def _assert_clip_contract(label, evidence, contract):
    exact = {
        'fully_outside_count': contract['fully_outside_count'],
        'fully_outside_object_ids_sha256': contract.get(
            'fully_outside_fids_sha256',
            contract.get('fully_outside_object_ids_sha256')),
        'geometry_clipped_count': contract['geometry_clipped_count'],
        'geometry_clipped_object_ids_sha256': contract.get(
            'geometry_clipped_fids_sha256',
            contract.get('geometry_clipped_object_ids_sha256')),
    }
    observed = {key: evidence.get(key) for key in exact}
    if observed != exact:
        raise RuntimeError(
            f'{label} authoritative clip inventory changed: '
            f'expected={exact}, observed={observed}')


def _stream_map35(path, geology_sequence, faults_sequence, clip):
    data_sources, descriptions = _load_lookups(path)
    outputs = {
        'MapUnitPolys': (geology_sequence, 'az_azgs_map35_geology'),
        'ContactsAndFaults': (faults_sequence, 'az_azgs_map35_faults'),
    }
    stats = {}
    for layer in ('MapUnitPolys', 'ContactsAndFaults'):
        contract = MAP35_LAYER_CONTRACTS[layer]
        is_geology = layer == 'MapUnitPolys'
        sequence, _ = outputs[layer]
        fids, source_ids, outside, clipped = [], [], [], []
        geometry_types = {}
        encoding_records = []
        pre = post = 0.0
        emitted_ids = []
        with fiona.open(path, layer=layer) as source, \
                open(sequence, 'w', encoding='utf-8') as output:
            for row in source:
                fid = _positive_integer(row.id, f'{layer} FID')
                properties = dict(row['properties'])
                source_id = properties.get(contract['source_id_field'])
                data_source_id = properties.get('DataSourceID')
                if (not isinstance(source_id, str) or not source_id or
                        not isinstance(data_source_id, str) or
                        data_source_id not in data_sources):
                    raise RuntimeError(f'{layer} row {fid} has broken source linkage')
                geometry = shapely_shape(row['geometry'])
                geometry_types[geometry.geom_type] = (
                    geometry_types.get(geometry.geom_type, 0) + 1)
                fids.append(fid)
                source_ids.append(source_id)
                metric = 'area' if is_geology else 'length'
                clipped_geometry, flags = _clip_geometry(
                    geometry, clip['native'], clip['native_prepared'],
                    {'Polygon'} if is_geology else {'LineString'}, metric)
                pre += flags['preclip_metric']
                post += flags['postclip_metric']
                if flags['outside']:
                    outside.append(fid)
                    continue
                if flags['changed']:
                    clipped.append(fid)
                if (not is_geology and
                        fid in MAP35_FAULT_ENCODING_EXCLUSIONS['fids']):
                    encoding_records.append({
                        'fid': fid, 'source_record_id': source_id,
                        'native_length_m': geometry.length,
                        'source_type': geometry.geom_type,
                        'coordinate_count': _line_coordinate_count(geometry),
                        'source_geometry_sha256': _geometry_sha256(geometry),
                    })
                    continue
                transformed = transform_geom(
                    'EPSG:26912', 'EPSG:4326',
                    # Fiona 1.9.5 drops the geometry type when its deprecated
                    # precision argument is nonnegative. Transform losslessly,
                    # then round once in _plain_wgs84_geometry instead.
                    shapely_mapping(clipped_geometry), precision=-1)
                plain = _plain_wgs84_geometry(
                    transformed,
                    {'Polygon', 'MultiPolygon'} if is_geology else
                    {'LineString', 'MultiLineString'})
                if is_geology:
                    unit = properties.get('MapUnit')
                    description = descriptions.get(unit)
                    if description is None:
                        raise RuntimeError(
                            f'MapUnitPolys row {fid} has no DMU row for {unit!r}')
                    normalized = _base_properties(
                        fid, 'azgs_map35_2025_gems',
                        f'azgs-map35-mapunit:{source_id}', source_id,
                        '1:1,000,000', 'publication_compilation_scale',
                        'Richard et al. (2000), AZGS Map 35; 2025 GeMS conversion',
                        MAP35_CATALOG_URL, 'AZGS Map 35 / AGMS-1749135591815-872')
                    normalized.update({
                        'source_link_id': data_source_id,
                        'source_link_status':
                            'retained_gems_id_catalog_details_absent',
                        'map_unit': unit, 'unit_name': _text(
                            description.get('Name'), 180),
                        'unit_full_name': _text(
                            description.get('FullName'), 240),
                        'age': _text(description.get('Age'), 120),
                        'geo_material': _text(
                            description.get('GeoMaterial'), 160),
                        'unit_description': _text(
                            description.get('Description'), 500),
                        'identity_confidence': _text(
                            properties.get('IdentityConfidence'), 80),
                        'symbol': _text(properties.get('Symbol'), 60),
                    })
                else:
                    normalized = _base_properties(
                        fid, 'azgs_map35_2025_gems',
                        f'azgs-map35-structure:{source_id}', source_id,
                        '1:1,000,000', 'publication_compilation_scale',
                        'Richard et al. (2000), AZGS Map 35; 2025 GeMS conversion',
                        MAP35_CATALOG_URL, 'AZGS Map 35 / AGMS-1749135591815-872')
                    normalized.update({
                        'source_link_id': data_source_id,
                        'source_link_status':
                            'retained_gems_id_catalog_details_absent',
                        'structure_type': _text(properties.get('Type'), 120),
                        'concealed': _text(properties.get('IsConcealed'), 20),
                        'location_confidence_m':
                            properties.get('LocationConfidenceMeters'),
                        'existence_confidence': _text(
                            properties.get('ExistenceConfidence'), 80),
                        'identity_confidence': _text(
                            properties.get('IdentityConfidence'), 80),
                        'symbol': _text(properties.get('Symbol'), 60),
                    })
                normalized = {key: value for key, value in normalized.items()
                              if value is not None}
                _write_feature(output, {
                    'type': 'Feature', 'id': fid,
                    'properties': normalized, 'geometry': plain})
                emitted_ids.append(fid)
        if (fids != list(range(1, contract['n'] + 1)) or
                _canonical_sha256([str(fid) for fid in fids]) !=
                contract['fid_sha256'] or
                len(source_ids) != len(set(source_ids)) or
                _canonical_sha256(sorted(source_ids)) !=
                contract['source_ids_sha256'] or
                geometry_types != contract['geometry_types']):
            raise RuntimeError(f'AZGS {layer} source feature inventory changed')
        clip_evidence = _clip_evidence(
            fids, clipped, outside, pre, post,
            'area_square_meters' if is_geology else 'length_meters')
        _assert_clip_contract(layer, clip_evidence, contract)
        exclusions = (None if is_geology else
                      _encoding_exclusion_evidence(encoding_records))
        expected_ids = (set(fids) - set(outside) -
                        (set(MAP35_FAULT_ENCODING_EXCLUSIONS['fids'])
                         if not is_geology else set()))
        if set(emitted_ids) != expected_ids or len(emitted_ids) != len(expected_ids):
            raise RuntimeError(f'AZGS {layer} emitted ID inventory changed')
        stats[layer] = {
            'source_records': len(fids), 'n': len(emitted_ids),
            'source_fids_sha256': contract['fid_sha256'],
            'source_ids_sha256': contract['source_ids_sha256'],
            'geometry_inventory': geometry_types,
            'empty_geometry_count': 0, 'empty_geometry_fids': [],
            'topology_repair': _no_repair_evidence(),
            'encoding_exclusions': exclusions,
            'spatial_clip': clip_evidence,
            'expected_tiled_ids': sorted(expected_ids),
        }
    return stats


def _selected_item_metadata(value):
    return {key: value.get(key) for key in (
        'id', 'title', 'type', 'owner', 'orgId', 'created', 'modified',
        'access', 'url', 'description', 'snippet', 'licenseInfo',
        'accessInformation', 'tags', 'typeKeywords', 'size')}


def _service_item_snapshot():
    selected = _selected_item_metadata(_request_json(
        AZGS_SERVICE_ITEM_URL, {'f': 'json'}))
    if (selected.get('id') != AZGS_SERVICE_ITEM_ID or
            selected.get('orgId') != 'Ezk9fcjSUkeadg6u' or
            selected.get('type') != 'Feature Service' or
            selected.get('access') != 'public' or
            'AZGS' not in (selected.get('tags') or [])):
        raise RuntimeError('AZGS ArcGIS service-item identity changed')
    digest = _canonical_sha256(selected)
    if digest != AZGS_SERVICE_ITEM_METADATA_SHA256:
        raise RuntimeError('AZGS ArcGIS service-item metadata changed')
    return {'metadata_sha256': digest, 'metadata': selected}


def _selected_layer_metadata(metadata):
    return {
        'id': metadata.get('id'), 'name': metadata.get('name'),
        'type': metadata.get('type'),
        'serviceItemId': metadata.get('serviceItemId'),
        'geometryType': metadata.get('geometryType'),
        'objectIdField': metadata.get('objectIdField'),
        'displayField': metadata.get('displayField'),
        'description': metadata.get('description'),
        'copyrightText': metadata.get('copyrightText'),
        'maxRecordCount': metadata.get('maxRecordCount'),
        'hasM': metadata.get('hasM'), 'hasZ': metadata.get('hasZ'),
        'hasAttachments': metadata.get('hasAttachments'),
        'editingInfo': metadata.get('editingInfo'),
        'extent': metadata.get('extent'),
        'spatialReference': metadata.get('spatialReference'),
        'fields': [
            {key: field.get(key) for key in
             ('name', 'type', 'alias', 'length', 'nullable')}
            for field in metadata.get('fields') or []
            if isinstance(field, dict)],
    }


def _layer_snapshot(key):
    contract = ARCGIS_LAYERS[key]
    metadata = _request_json(contract['url'], {'f': 'json'})
    selected = _selected_layer_metadata(metadata)
    oid_fields = [field for field in selected['fields']
                  if field.get('type') == 'esriFieldTypeOID']
    if (selected.get('id') != contract['layer_id'] or
            selected.get('name') != contract['name'] or
            selected.get('geometryType') != contract['geometry_type'] or
            selected.get('serviceItemId') != AZGS_SERVICE_ITEM_ID or
            selected.get('objectIdField') != contract['oid_field'] or
            len(oid_fields) != 1 or
            oid_fields[0].get('name') != contract['oid_field']):
        raise RuntimeError(f'{key} typed ArcGIS layer identity changed')
    metadata_sha = _canonical_sha256(selected)
    if metadata_sha != contract['layer_metadata_sha256']:
        raise RuntimeError(f'{key} selected ArcGIS metadata changed')
    ids_result = _request_json(contract['url'] + '/query', {
        'where': '1=1', 'returnIdsOnly': 'true', 'f': 'json'})
    if ids_result.get('objectIdFieldName') != contract['oid_field']:
        raise RuntimeError(f'{key} returnIdsOnly OID field changed')
    raw_ids = ids_result.get('objectIds')
    if not isinstance(raw_ids, list):
        raise RuntimeError(f'{key} returnIdsOnly response has no ID list')
    ids = sorted(_positive_integer(value, contract['oid_field'])
                 for value in raw_ids)
    observed = {
        'object_id_field': contract['oid_field'], 'n': len(ids),
        'minimum_object_id': ids[0] if ids else None,
        'maximum_object_id': ids[-1] if ids else None,
        'object_ids_sha256': _canonical_sha256(ids),
        'layer_metadata_sha256': metadata_sha,
    }
    expected = {
        'object_id_field': contract['oid_field'],
        **{field: contract[field] for field in observed
           if field != 'object_id_field'},
    }
    if len(ids) != len(set(ids)) or observed != expected:
        raise RuntimeError(
            f'{key} live ArcGIS snapshot changed: '
            f'expected={expected}, observed={observed}')
    return {'ids': ids, 'metadata': selected, **observed}


def _ci_properties(feature):
    properties = feature.get('properties') or {}
    if not isinstance(properties, dict):
        raise RuntimeError('ArcGIS feature properties are not an object')
    return {str(key).casefold(): value for key, value in properties.items()}


def _iter_snapshot(key, snapshot, page=250):
    contract = ARCGIS_LAYERS[key]
    ids = snapshot['ids']
    emitted = 0
    for start in range(0, len(ids), page):
        expected = ids[start:start + page]
        value = _request_json(contract['url'] + '/query', {
            'objectIds': ','.join(map(str, expected)),
            'outFields': ','.join(contract['fields']),
            'returnGeometry': 'true', 'returnTrueCurves': 'false',
            'outSR': 4326, 'geometryPrecision': 8,
            'orderByFields': contract['oid_field'] + ' ASC', 'f': 'geojson',
        }, post=True)
        features = value.get('features')
        if not isinstance(features, list):
            raise RuntimeError(f'{key} ArcGIS page has no feature array')
        actual = [_positive_integer(
            _ci_properties(feature).get(contract['oid_field'].casefold()),
            contract['oid_field']) for feature in features]
        if actual != expected:
            raise RuntimeError(
                f'{key} ArcGIS page mismatch at {start}: '
                f'expected={expected}, actual={actual}')
        yield from features
        emitted += len(features)
    if emitted != len(ids):
        raise RuntimeError(f'{key} emitted {emitted}; snapshot has {len(ids)}')


def _raw_arcgis_record(key, feature):
    contract = ARCGIS_LAYERS[key]
    properties = feature.get('properties') or {}
    oid = _positive_integer(
        _ci_properties(feature).get(contract['oid_field'].casefold()),
        contract['oid_field'])
    return {
        'id': oid,
        'properties': {field: properties.get(field)
                       for field in contract['fields']},
        'geometry': feature.get('geometry'),
    }


def _arcgis_content_digest(key, snapshot):
    records = [_raw_arcgis_record(key, feature)
               for feature in _iter_snapshot(key, snapshot)]
    return _canonical_sha256(records)


def _normalize_district(oid, properties):
    district_name = _text(properties.get('d_name'), 120)
    if not district_name:
        raise RuntimeError(f'AZGS district {oid} has no district name')
    result = _base_properties(
        oid, 'azgs_mining_districts_2021', f'azgs-district:{oid}', str(oid),
        'not stated', 'official_service_metadata_absent',
        'AZGS Mining Districts', ARCGIS_LAYERS[
            'az_azgs_mining_districts']['url'],
        'AZGS Mining Districts ArcGIS item beef607714624113b8f69c2a4bbc6a2d')
    result.update({
        'district_id': _text(properties.get('id'), 30),
        'district_name': district_name,
        'county_source': _text(properties.get('county'), 80),
        'other_name': _text(properties.get('other_name'), 250),
        'reference': _text(properties.get('reference'), 250),
        'commodities': _text(properties.get('comms'), 120),
        'physiography': _text(properties.get('phys'), 120),
    })
    return {key: value for key, value in result.items() if value is not None}


def _normalize_occurrence(oid, properties):
    name = _text(properties.get('site_name'), 180)
    if not name:
        raise RuntimeError(f'AZGS occurrence {oid} has no site name')
    result = _base_properties(
        oid, 'azgs_compiled_critical_minerals_2021',
        f'azgs-critical-mineral:{oid}', str(oid),
        'N/A (point occurrence)', 'not_applicable_point',
        'AZGS compiled critical-mineral resources/deposits/occurrences',
        ARCGIS_LAYERS['az_azgs_critical_minerals']['url'],
        'AZGS Mining Districts ArcGIS item beef607714624113b8f69c2a4bbc6a2d')
    aliases = {
        'site_name': 'site_name', 'commodity': 'commodity',
        'resource': 'resource', 'res_appxcont': 'approx_content',
        'res_appxcontut': 'approx_content_units', 'ref_link': 'reference_url',
        'citation': 'citation', 'notes': 'notes',
        'host_rocks_age___lithology': 'host_rocks',
        'igneous_rocks': 'igneous_rocks',
        'high_temperature_alteration': 'high_temp_alteration',
        'structural_geology': 'structural_geology',
        'mineralization_age': 'mineralization_age',
        'low_temperature_alteration': 'low_temp_alteration',
        'gangue_and_ore_mineralogy': 'ore_mineralogy',
        'deposit_type': 'deposit_type',
        'old_district_name': 'old_district_name',
        'new_district_name': 'new_district_name',
    }
    for source, target in aliases.items():
        result[target] = _text(properties.get(source), 500)
    result['site_name'] = name
    return {key: value for key, value in result.items() if value is not None}


def _stream_arcgis(key, snapshot, sequence, clip):
    contract = ARCGIS_LAYERS[key]
    is_district = key == 'az_azgs_mining_districts'
    records = []
    outside, clipped, empty, emitted_ids = [], [], [], []
    pre = post = 0.0
    with open(sequence, 'w', encoding='utf-8') as output:
        for raw in _iter_snapshot(key, snapshot):
            record = _raw_arcgis_record(key, raw)
            records.append(record)
            oid = record['id']
            properties = _ci_properties(raw)
            geometry_value = raw.get('geometry')
            if not geometry_value or not list(_positions(
                    geometry_value.get('coordinates'))):
                empty.append(oid)
                continue
            geometry = shapely_shape(geometry_value)
            if is_district:
                clipped_geometry, flags = _clip_geometry(
                    geometry, clip['wgs84'], clip['wgs84_prepared'],
                    {'Polygon'}, 'area')
                pre += flags['preclip_metric']
                post += flags['postclip_metric']
                if flags['outside']:
                    outside.append(oid)
                    continue
                if flags['changed']:
                    clipped.append(oid)
                plain = _plain_wgs84_geometry(
                    shapely_mapping(clipped_geometry),
                    {'Polygon', 'MultiPolygon'})
                normalized = _normalize_district(oid, properties)
            else:
                if geometry.geom_type != 'Point' or geometry.is_empty:
                    raise RuntimeError(f'AZGS occurrence {oid} is not a point')
                if not clip['wgs84'].covers(geometry):
                    outside.append(oid)
                    continue
                pre += 1
                post += 1
                plain = _plain_wgs84_geometry(
                    shapely_mapping(geometry), {'Point'})
                normalized = _normalize_occurrence(oid, properties)
            _write_feature(output, {
                'type': 'Feature', 'id': oid,
                'properties': normalized, 'geometry': plain})
            emitted_ids.append(oid)
    digest = _canonical_sha256(records)
    if digest != contract['source_content_sha256']:
        raise RuntimeError(f'{key} source content changed: {digest}')
    if empty:
        raise RuntimeError(f'{key} gained empty geometries: {empty}')
    evidence = _clip_evidence(
        snapshot['ids'], clipped, outside, pre, post,
        'area_square_degrees' if is_district else 'point_count',
        ordering='validate_in_epsg4326_then_state_intersection')
    _assert_clip_contract(key, evidence, contract)
    expected_ids = set(snapshot['ids']) - set(outside)
    if set(emitted_ids) != expected_ids or len(emitted_ids) != len(expected_ids):
        raise RuntimeError(f'{key} emitted object IDs do not reconcile')
    return {
        'source_records': len(snapshot['ids']), 'n': len(emitted_ids),
        'empty_geometry_count': 0, 'empty_geometry_object_ids': [],
        'topology_repair': _no_repair_evidence(
            'validate_in_epsg4326_then_state_intersection')
            if is_district else None,
        'spatial_clip': evidence, 'source_content_sha256': digest,
        'expected_tiled_ids': sorted(expected_ids),
    }


def _tippecanoe_version():
    try:
        completed = subprocess.run(
            ['tippecanoe', '--version'], check=True, capture_output=True,
            text=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RuntimeError(f'tippecanoe version check failed: {exc}') from exc
    output = (completed.stdout + completed.stderr).strip()
    match = re.fullmatch(r'tippecanoe (v\d+\.\d+\.\d+)', output)
    if match is None:
        raise RuntimeError(f'unrecognized tippecanoe version: {output!r}')
    return match.group(1)


def _run_tippecanoe(output, layers, attribution, maxzoom=12):
    build_directory = os.path.realpath(os.path.dirname(output))
    if (not output.endswith('.pmtiles') or
            any(os.path.realpath(os.path.dirname(sequence)) != build_directory
                for _, sequence in layers)):
        raise RuntimeError(
            'Tippecanoe output and every input must share one private directory')
    command = [
        # Run with relative basenames so PMTiles generator_options never leaks
        # a random staging path and independent builds remain byte-identical.
        'tippecanoe', '--force', '--output', os.path.basename(output),
        '--minimum-zoom=0', f'--maximum-zoom={maxzoom}',
        '--full-detail=12', '--no-feature-limit', '--no-tile-size-limit',
        '--drop-rate=1',
        '--no-tiny-polygon-reduction-at-maximum-zoom',
        # Do not use --read-parallel: with two named input layers Tippecanoe
        # can interleave records differently and produce byte-distinct PMTiles.
        '--simplify-only-low-zooms', '--quiet',
        f'--name={attribution}', f'--description={attribution}',
        f'--attribution={attribution}',
    ]
    for layer, sequence in layers:
        command.extend(('-L', f'{layer}:{os.path.basename(sequence)}'))
    subprocess.run(command, check=True, cwd=build_directory)


def _build_reproducible(first, second, layers, attribution):
    if not first.endswith('.pmtiles') or not second.endswith('.pmtiles'):
        raise RuntimeError(
            'both deterministic comparison targets must end in .pmtiles')
    # Run both builds at the identical path. Tippecanoe otherwise derives a
    # different archive name from the comparison filename, creating harmless
    # but byte-distinct metadata that masks real reproducibility.
    _run_tippecanoe(first, layers, attribution)
    os.replace(first, second)
    _run_tippecanoe(first, layers, attribution)
    first_identity = (os.path.getsize(first), _sha256(first))
    second_identity = (os.path.getsize(second), _sha256(second))
    if first_identity != second_identity:
        raise RuntimeError(
            'Arizona PMTiles build is not byte reproducible: '
            f'{first_identity} != {second_identity}')
    return {'bytes': first_identity[0], 'sha256': first_identity[1],
            'double_build': 'byte_identical'}


def _raw_pmtiles_metadata(path):
    with open(path, 'rb') as source:
        header = source.read(127)
        if len(header) != 127 or header[:8] != b'PMTiles\x03':
            raise RuntimeError(f'{path} is not PMTiles v3')
        values = struct.unpack_from('<11Q', header, 8)
        offset, length = values[2], values[3]
        if offset < 127 or length <= 0 or offset + length > os.path.getsize(path):
            raise RuntimeError(f'{path} has invalid PMTiles metadata bounds')
        source.seek(offset)
        raw = source.read(length)
    compression = header[97]
    if compression == 2:
        try:
            raw = gzip.decompress(raw)
        except (OSError, EOFError) as exc:
            raise RuntimeError(f'{path} has invalid gzip metadata') from exc
    elif compression != 1:
        raise RuntimeError(f'{path} has unsupported metadata compression')
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f'{path} has invalid PMTiles metadata JSON') from exc
    if not isinstance(value, dict):
        raise RuntimeError(f'{path} PMTiles metadata is not an object')
    return value


def _validate_path_free_metadata(path, expected_label):
    metadata = _raw_pmtiles_metadata(path)
    options = metadata.get('generator_options')
    if (metadata.get('name') != expected_label or
            metadata.get('description') != expected_label or
            metadata.get('attribution') != expected_label or
            metadata.get('generator') != f'tippecanoe {TIPPECANOE_VERSION}' or
            not isinstance(options, str) or '/' in options or '\\' in options or
            '.staging' in options or 'nwmm-az-baselines-' in options or
            re.search(r'(^|\s)\.\.?($|\s)', options)):
        raise RuntimeError(
            f'{path} PMTiles metadata contains an unstable build path or label')
    return {
        'status': 'complete_path_free_reproducible_metadata',
        'metadata_sha256': _canonical_sha256(metadata),
        'name': metadata['name'], 'description': metadata['description'],
        'generator': metadata['generator'],
        'generator_options_sha256': _canonical_sha256(options),
    }


def _validate_pmtiles(path, layers, expected_label, *, pmtiles_header=None):
    if pmtiles_header is None:
        from validate_national import _pmtiles_header as pmtiles_header
    requirements = {layer: list(REQUIRED_PROVENANCE) for layer in layers}
    metadata = pmtiles_header(
        path, list(layers), requirements, verify_feature_properties=True,
        expected_state='AZ', expected_bounds=[AZ_BOUNDS],
        collect_feature_ids=True)
    if set(metadata.get('source_layers') or []) != set(layers):
        raise RuntimeError(f'{path} has unexpected source layers')
    bounds = metadata['bounds']
    if (bounds[0] < AZ_BOUNDS[0] or bounds[1] < AZ_BOUNDS[1] or
            bounds[2] > AZ_BOUNDS[2] or bounds[3] > AZ_BOUNDS[3]):
        raise RuntimeError(f'{path} PMTiles bounds escape Arizona: {bounds}')
    if any(metadata['semantic_layer_counts'].get(layer, 0) <= 0
           for layer in layers):
        raise RuntimeError(f'{path} has an empty semantic source layer')
    metadata['reproducible_metadata'] = _validate_path_free_metadata(
        path, expected_label)
    return metadata


def _id_inventory(metadata, layer, expected_ids):
    observed = metadata.get('maxzoom_feature_ids', {}).get(layer, [])
    instances = metadata.get('maxzoom_feature_instances', {}).get(layer)
    expected = sorted(expected_ids)
    observed_set = set(observed)
    if (observed_set != set(expected) or len(observed_set) != len(expected) or
            not isinstance(instances, int) or instances < len(expected)):
        raise RuntimeError(
            f'{layer} unique maxzoom IDs do not reconcile: '
            f'missing={sorted(set(expected) - observed_set)[:100]}, '
            f'extra={sorted(observed_set - set(expected))[:100]}')
    return {
        'status': 'complete', 'expected_unique_tiled_ids': len(expected),
        'expected_ids_sha256': _canonical_sha256(expected),
        'maxzoom_unique_tiled_ids': len(observed_set),
        'maxzoom_feature_instances': instances,
        'maxzoom_ids_sha256': _canonical_sha256(sorted(observed_set)),
    }


def _artifact_fields(path, metadata, identity):
    return {
        **identity, 'bounds': metadata['bounds'],
        'semantic_tile_feature_counts': metadata['semantic_layer_counts'],
        'reproducible_metadata': metadata['reproducible_metadata'],
    }


def _snapshot_manifest(snapshot):
    return {key: snapshot[key] for key in (
        'object_id_field', 'n', 'minimum_object_id', 'maximum_object_id',
        'object_ids_sha256', 'layer_metadata_sha256')}


def _with_clip_authority(stats, clip_manifest):
    result = dict(clip_manifest)
    result.update(stats['spatial_clip'])
    return result


def _map35_entry(stats, schema, catalog, member, wrapper, clip_manifest,
                 artifact, inventories):
    by_layer = {}
    for source_name, tile_layer in (
            ('MapUnitPolys', MAP35_LAYERS[0]),
            ('ContactsAndFaults', MAP35_LAYERS[1])):
        item = dict(stats[source_name])
        item.pop('expected_tiled_ids')
        item['spatial_clip'] = _with_clip_authority(
            stats[source_name], clip_manifest)
        item['source_id_inventory'] = inventories[tile_layer]
        by_layer[source_name] = item
    n = sum(item['n'] for item in stats.values())
    return {
        'schema_version': 1, 'status': 'baseline_not_release',
        'state': 'AZ', 'file': os.path.relpath(MAP35_OUT, SITE),
        'format': 'pmtiles', 'source_layers': list(MAP35_LAYERS),
        'source': {
            'title': 'Geologic Map of Arizona - 2000 (2025 GeMS conversion)',
            'authority': 'Arizona Geological Survey',
            'publication_id': 'AZGS Map 35 / AGMS-1749135591815-872',
            'catalog_url': MAP35_CATALOG_URL,
            'metadata_api_url': MAP35_METADATA_URL,
            'collection_url': MAP35_COLLECTION_URL,
            'catalog_metadata_sha256': catalog['metadata_sha256'],
            'extracted_member': member['member'],
            'extracted_member_bytes': member['bytes'],
            'extracted_member_sha256': member['sha256'],
            'wrapper_observed_bytes': wrapper['bytes'],
            'wrapper_observed_sha256': wrapper['sha256'],
            'wrapper_identity_status': wrapper['identity_status'],
            'native_crs': 'EPSG:26912', 'source_scale': '1:1,000,000',
        },
        'source_schema': schema,
        'n': n, 'states': {'AZ': n},
        'raw_source_records': sum(item['source_records'] for item in stats.values()),
        'by_layer': by_layer, 'retrieved': TODAY,
        'required_properties': list(REQUIRED_PROVENANCE),
        'provenance_note': (
            'Every polygon and structure carries Map 35 publication scale and '
            'its GeMS DataSourceID linkage. The package DataSources table '
            'contains identifiers but no source citation or native scale, so '
            'polygon-level larger-scale replacement remains explicitly '
            'pending. Two sub-2-meter structural artifacts below z12 encoding '
            'resolution and two out-of-state traces are exact-inventoried.'),
        **artifact,
    }


def _arcgis_entry(key, snapshot, stats, service_item, clip_manifest,
                  artifact, inventory):
    contract = ARCGIS_LAYERS[key]
    is_district = key == 'az_azgs_mining_districts'
    source_inventory = dict(stats)
    source_inventory.pop('spatial_clip')
    source_inventory.pop('expected_tiled_ids')
    source_inventory['source_id_inventory'] = inventory
    return {
        'schema_version': 1, 'status': 'baseline_not_release',
        'state': 'AZ', 'file': os.path.relpath(BASELINE_KEYS[key], SITE),
        'format': 'pmtiles', 'source_layer': contract['source_layer'],
        'source': {
            'title': contract['name'], 'authority': 'Arizona Geological Survey',
            'service_layer': contract['url'],
            'service_item_id': AZGS_SERVICE_ITEM_ID,
            'service_item_metadata_sha256': service_item['metadata_sha256'],
            'source_scale': ('not stated' if is_district else
                             'N/A (point occurrence)'),
        },
        'n': stats['n'], 'states': {'AZ': stats['n']},
        'retrieved': TODAY, 'snapshot': _snapshot_manifest(snapshot),
        'source_inventory': source_inventory,
        'spatial_clip': _with_clip_authority(stats, clip_manifest),
        'required_properties': list(REQUIRED_PROVENANCE),
        'provenance_note': (
            'Official service scale is not stated; no map scale is invented.'
            if is_district else
            'Point occurrences have no map-scale term. Public contact fields '
            'are intentionally excluded from the browser baseline.'),
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
    os.makedirs(OUT_DIR, exist_ok=True)
    handle, pending_manifest = tempfile.mkstemp(
        prefix='.manifest-az-state-survey-',
        dir=os.path.dirname(MANIFEST))
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
                raise RuntimeError('public manifest changed during Arizona build')
        for key, final_path in BASELINE_KEYS.items():
            source_path = pending[key]
            backup = os.path.join(
                os.path.dirname(source_path), f'previous-{key}.pmtiles')
            if os.path.exists(final_path):
                os.replace(final_path, backup)
                backups[final_path] = backup
            os.replace(source_path, final_path)
            installed.append(final_path)
        with open(MANIFEST, 'rb') as current:
            if hashlib.sha256(current.read()).hexdigest() != manifest_sha:
                raise RuntimeError(
                    'public manifest changed during Arizona publication')
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


def _entry_layers(entry):
    return ([entry['source_layer']] if entry.get('source_layer') else
            entry['source_layers'])


def _expected_ids_for_key(key):
    if key == 'az_azgs_map35_2025':
        geology = set(range(1, MAP35_LAYER_CONTRACTS['MapUnitPolys']['n'] + 1))
        faults = set(range(
            1, MAP35_LAYER_CONTRACTS['ContactsAndFaults']['n'] + 1))
        faults -= {10_075, 15_427}
        faults -= set(MAP35_FAULT_ENCODING_EXCLUSIONS['fids'])
        return {MAP35_LAYERS[0]: geology, MAP35_LAYERS[1]: faults}
    contract = ARCGIS_LAYERS[key]
    return {contract['source_layer']: set(range(
        contract['minimum_object_id'], contract['maximum_object_id'] + 1))}


def _verify_entry(key, entry, *, pmtiles_header=None):
    expected_path = BASELINE_KEYS[key]
    layers = _entry_layers(entry)
    if (entry.get('schema_version') != 1 or
            entry.get('status') != 'baseline_not_release' or
            entry.get('state') != 'AZ' or entry.get('format') != 'pmtiles' or
            entry.get('file') != os.path.relpath(expected_path, SITE) or
            not isinstance(layers, list) or not layers):
        raise RuntimeError(f'{key} baseline manifest schema is invalid')
    if not os.path.isfile(expected_path):
        raise RuntimeError(f'{key} PMTiles artifact is missing')
    metadata = _validate_pmtiles(
        expected_path, layers, ARCHIVE_LABELS[key],
        pmtiles_header=pmtiles_header)
    if (entry.get('bytes') != os.path.getsize(expected_path) or
            entry.get('sha256') != _sha256(expected_path) or
            entry.get('bounds') != metadata['bounds'] or
            entry.get('semantic_tile_feature_counts') !=
            metadata['semantic_layer_counts'] or
            entry.get('reproducible_metadata') !=
            metadata['reproducible_metadata']):
        raise RuntimeError(f'{key} artifact identity/semantics changed')
    expected_ids = _expected_ids_for_key(key)
    for layer, ids in expected_ids.items():
        _id_inventory(metadata, layer, ids)
    if entry.get('required_properties') != list(REQUIRED_PROVENANCE):
        raise RuntimeError(f'{key} required provenance schema is invalid')
    n = sum(len(ids) for ids in expected_ids.values())
    if entry.get('n') != n or entry.get('states') != {'AZ': n}:
        raise RuntimeError(f'{key} feature count does not reconcile')
    return {'features': n, 'bytes': entry['bytes'], 'sha256': entry['sha256']}


def validate_manifest_baselines(manifest, *, pmtiles_header=None):
    """Offline exact validator; suitable for a later national CI hook."""
    baselines = manifest.get('national_baselines')
    if not isinstance(baselines, dict):
        raise RuntimeError('manifest national_baselines is missing')
    present = set(BASELINE_KEYS) & set(baselines)
    if present and present != set(BASELINE_KEYS):
        raise RuntimeError('Arizona state-survey baseline must be an atomic set')
    if not present:
        raise RuntimeError('Arizona state-survey baseline set is absent')
    result = {}
    for key in BASELINE_KEYS:
        result[key] = _verify_entry(
            key, baselines[key], pmtiles_header=pmtiles_header)
    map35 = baselines['az_azgs_map35_2025']
    source = map35.get('source') or {}
    if (source.get('extracted_member_bytes') != MAP35_MEMBER_BYTES or
            source.get('extracted_member_sha256') != MAP35_MEMBER_SHA256 or
            source.get('catalog_metadata_sha256') !=
            MAP35_CATALOG_METADATA_SHA256 or
            source.get('wrapper_identity_status') !=
            'dynamic_transport_wrapper_not_source_identity'):
        raise RuntimeError('Arizona Map 35 source identity is invalid')
    for key, contract in ARCGIS_LAYERS.items():
        entry = baselines[key]
        expected_snapshot = {
            'object_id_field': contract['oid_field'],
            **{field: contract[field] for field in (
                'n', 'minimum_object_id', 'maximum_object_id',
                'object_ids_sha256', 'layer_metadata_sha256')},
        }
        if (entry.get('snapshot') != expected_snapshot or
                entry.get('source', {}).get(
                    'service_item_metadata_sha256') !=
                AZGS_SERVICE_ITEM_METADATA_SHA256 or
                entry.get('source_inventory', {}).get(
                    'source_content_sha256') !=
                contract['source_content_sha256']):
            raise RuntimeError(f'{key} source snapshot evidence is invalid')
    return result


def check():
    _, manifest = _strict_manifest_bytes()
    result = validate_manifest_baselines(manifest)
    print(json.dumps(result, indent=2))
    return result


def build(*, publish=False, grace_seconds=0):
    if not shutil.which('tippecanoe'):
        raise RuntimeError('tippecanoe v2.79.0 with PMTiles output is required')
    observed_tippecanoe = _tippecanoe_version()
    if observed_tippecanoe != TIPPECANOE_VERSION:
        raise RuntimeError(
            f'tippecanoe changed from {TIPPECANOE_VERSION} to '
            f'{observed_tippecanoe}; re-audit encoding contracts')
    if fiona is None or transform_geom is None:
        raise RuntimeError(
            'Fiona is required (workspace geospatial Python: '
            '/Users/matthewlew/miniconda3/bin/python)')
    if not 0 <= grace_seconds <= 60:
        raise RuntimeError('manifest grace must be from 0 to 60 seconds')
    if not publish and grace_seconds:
        raise RuntimeError('manifest grace applies only to --publish')
    from validate_national import _pmtiles_header  # noqa: F401

    _ensure_private_staging_root()
    with tempfile.TemporaryDirectory(
            prefix='nwmm-az-baselines-', dir=PRIVATE_STAGING_ROOT) as temp:
        clip = _load_az_clip()
        catalog = _catalog_snapshot()
        wrapper_path = os.path.join(temp, 'map35-collection.zip')
        wrapper = _download_collection(wrapper_path)
        gpkg_path = os.path.join(temp, 'AZStatewide.gpkg')
        member = _extract_pinned_geopackage(wrapper_path, gpkg_path)
        schema = _validate_geopackage_schema(gpkg_path)

        geology_sequence = os.path.join(temp, 'map35-geology.geojsonseq')
        faults_sequence = os.path.join(temp, 'map35-faults.geojsonseq')
        map35_stats = _stream_map35(
            gpkg_path, geology_sequence, faults_sequence, clip)

        service_item = _service_item_snapshot()
        snapshots = {}
        arc_stats = {}
        arc_sequences = {}
        for key in ARCGIS_LAYERS:
            snapshot = _layer_snapshot(key)
            snapshots[key] = snapshot
            sequence = os.path.join(temp, key + '.geojsonseq')
            arc_sequences[key] = sequence
            stats = _stream_arcgis(key, snapshot, sequence, clip)
            # Full second pass catches in-place content mutation with stable IDs.
            second_digest = _arcgis_content_digest(key, snapshot)
            if second_digest != stats['source_content_sha256']:
                raise RuntimeError(f'{key} mutated during full second pass')
            postflight = _layer_snapshot(key)
            if _snapshot_manifest(postflight) != _snapshot_manifest(snapshot):
                raise RuntimeError(f'{key} changed during postflight snapshot')
            arc_stats[key] = stats

        pending = {
            'az_azgs_map35_2025': os.path.join(temp, 'azgs-map35.pmtiles'),
            'az_azgs_mining_districts': os.path.join(
                temp, 'azgs-mining-districts.pmtiles'),
            'az_azgs_critical_minerals': os.path.join(
                temp, 'azgs-critical-minerals.pmtiles'),
        }
        # Tippecanoe infers PMTiles versus MBTiles from the final suffix.
        second = {key: path + '.second.pmtiles'
                  for key, path in pending.items()}
        identities = {}
        identities['az_azgs_map35_2025'] = _build_reproducible(
            pending['az_azgs_map35_2025'], second['az_azgs_map35_2025'],
            ((MAP35_LAYERS[0], geology_sequence),
             (MAP35_LAYERS[1], faults_sequence)),
            ARCHIVE_LABELS['az_azgs_map35_2025'])
        identities['az_azgs_mining_districts'] = _build_reproducible(
            pending['az_azgs_mining_districts'],
            second['az_azgs_mining_districts'],
            ((ARCGIS_LAYERS['az_azgs_mining_districts']['source_layer'],
              arc_sequences['az_azgs_mining_districts']),),
            ARCHIVE_LABELS['az_azgs_mining_districts'])
        identities['az_azgs_critical_minerals'] = _build_reproducible(
            pending['az_azgs_critical_minerals'],
            second['az_azgs_critical_minerals'],
            ((ARCGIS_LAYERS['az_azgs_critical_minerals']['source_layer'],
              arc_sequences['az_azgs_critical_minerals']),),
            ARCHIVE_LABELS['az_azgs_critical_minerals'])

        metadata = {
            'az_azgs_map35_2025': _validate_pmtiles(
                pending['az_azgs_map35_2025'], MAP35_LAYERS,
                ARCHIVE_LABELS['az_azgs_map35_2025']),
            'az_azgs_mining_districts': _validate_pmtiles(
                pending['az_azgs_mining_districts'],
                (ARCGIS_LAYERS['az_azgs_mining_districts']['source_layer'],),
                ARCHIVE_LABELS['az_azgs_mining_districts']),
            'az_azgs_critical_minerals': _validate_pmtiles(
                pending['az_azgs_critical_minerals'],
                (ARCGIS_LAYERS['az_azgs_critical_minerals']['source_layer'],),
                ARCHIVE_LABELS['az_azgs_critical_minerals']),
        }
        inventories = {}
        inventories.update({
            layer: _id_inventory(
                metadata['az_azgs_map35_2025'], layer,
                map35_stats[source]['expected_tiled_ids'])
            for source, layer in (
                ('MapUnitPolys', MAP35_LAYERS[0]),
                ('ContactsAndFaults', MAP35_LAYERS[1]))})
        for key in ARCGIS_LAYERS:
            layer = ARCGIS_LAYERS[key]['source_layer']
            inventories[layer] = _id_inventory(
                metadata[key], layer, arc_stats[key]['expected_tiled_ids'])

        artifacts = {
            key: _artifact_fields(pending[key], metadata[key], identities[key])
            for key in BASELINE_KEYS}
        entries = {
            'az_azgs_map35_2025': _map35_entry(
                map35_stats, schema, catalog, member, wrapper,
                clip['manifest'], artifacts['az_azgs_map35_2025'], inventories),
            'az_azgs_mining_districts': _arcgis_entry(
                'az_azgs_mining_districts',
                snapshots['az_azgs_mining_districts'],
                arc_stats['az_azgs_mining_districts'], service_item,
                clip['manifest'], artifacts['az_azgs_mining_districts'],
                inventories[ARCGIS_LAYERS[
                    'az_azgs_mining_districts']['source_layer']]),
            'az_azgs_critical_minerals': _arcgis_entry(
                'az_azgs_critical_minerals',
                snapshots['az_azgs_critical_minerals'],
                arc_stats['az_azgs_critical_minerals'], service_item,
                clip['manifest'], artifacts['az_azgs_critical_minerals'],
                inventories[ARCGIS_LAYERS[
                    'az_azgs_critical_minerals']['source_layer']]),
        }
        summary = {
            key: {
                'artifact': os.path.relpath(BASELINE_KEYS[key], SITE),
                'features': entries[key]['n'],
                'bytes': entries[key]['bytes'],
                'sha256': entries[key]['sha256'],
                'bounds': entries[key]['bounds'],
                'mode': 'published' if publish else 'private_audit_only',
            }
            for key in BASELINE_KEYS}
        if publish:
            print(
                f'Arizona archives validated; manifest stamp begins in '
                f'{grace_seconds} seconds')
            if grace_seconds:
                time.sleep(grace_seconds)
            _publish(pending, entries)
        print(json.dumps(summary, indent=2))
        return {'summary': summary, 'entries': entries}


def main(argv=None):
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--audit', action='store_true',
                      help='private full double build; do not publish (default)')
    mode.add_argument('--publish', action='store_true',
                      help='explicitly publish the atomic baseline set')
    mode.add_argument('--check', action='store_true',
                      help='offline exact validation of an existing publication')
    parser.add_argument('--manifest-grace-seconds', type=int, default=0,
                        help='0..60 second coordination window before publish')
    args = parser.parse_args(argv)
    if args.check:
        if args.manifest_grace_seconds:
            parser.error('--manifest-grace-seconds requires --publish')
        check()
    else:
        build(publish=args.publish,
              grace_seconds=args.manifest_grace_seconds)


if __name__ == '__main__':
    main()
