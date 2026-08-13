#!/usr/bin/env python3
"""Build lossless Colorado state-survey baselines without releasing Colorado.

The builder keeps three scientifically distinct products separate:

* the Tweto (1979) ``MapSourceID=map50`` polygons and faults exposed by the
  official USGS Cooperative National Geologic Map (CNGM) Feature Service;
* the Colorado Geological Survey (CGS) ON-006-15M Quaternary-fault and
  Cenozoic-fault layers, retained as separate MVT source layers; and
* the checksum-pinned CGS ON-007-08D historic metal-mining-district ZIP.

Every source is read twice into private GeoJSON sequences, and those sequences
must be byte-identical.  Tippecanoe is then run twice and both complete PMTiles
sets must be byte-identical and pass a full max-zoom ID/property scan.  Raw
statewide vectors never enter ``site/``.  The default command is private and
deletes its temporary products after printing the audit.  ``--publish`` is an
explicit, atomic manifest/archive operation intended for use only after source
review; it never changes ``states/CO.yaml`` or a release/DONE flag.
"""
from __future__ import annotations

import argparse
from collections import Counter
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

try:
    import fiona
except ImportError:  # pragma: no cover - exercised by build preflight
    fiona = None

try:
    import shapely
    from shapely import make_valid as shapely_make_valid
    from shapely.geometry import mapping as shapely_mapping
    from shapely.geometry import shape as shapely_shape
    from shapely.ops import transform as shapely_transform
    from shapely.ops import unary_union as shapely_unary_union
    from shapely.prepared import prep as shapely_prepare
    from shapely.validation import explain_validity as shapely_explain_validity
except ImportError:  # pragma: no cover - exercised by build preflight
    shapely = None
    shapely_make_valid = None
    shapely_mapping = None
    shapely_shape = None
    shapely_transform = None
    shapely_unary_union = None
    shapely_prepare = None
    shapely_explain_validity = None

try:
    from pyproj import Transformer
except ImportError:  # pragma: no cover - exercised by build preflight
    Transformer = None


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
SITE = os.path.join(ROOT, 'site')
MANIFEST = os.path.join(SITE, 'data', 'manifest.json')
PRIVATE_STAGING_ROOT = os.path.join(ROOT, 'build-inputs', '.staging')
STATE_CLIPS = os.path.join(ROOT, 'infra', 'state_clips.json')
OUT_DIR = os.path.join(SITE, 'data', 'tiles', 'states', 'co')

CNGM_OUT = os.path.join(OUT_DIR, 'usgs-cngm-tweto-500k.pmtiles')
ON006_OUT = os.path.join(OUT_DIR, 'cgs-on006-faults.pmtiles')
DISTRICTS_OUT = os.path.join(OUT_DIR, 'cgs-on007-districts.pmtiles')
BASELINE_KEYS = {
    'co_usgs_cngm_tweto_500k': CNGM_OUT,
    'co_cgs_on006_faults': ON006_OUT,
    'co_cgs_on007_districts': DISTRICTS_OUT,
}

CNGM_ITEM_ID = '8323586344b747c6b44731d399ec1307'
CNGM_ITEM_URL = (
    f'https://www.arcgis.com/sharing/rest/content/items/{CNGM_ITEM_ID}')
CNGM_SERVICE = (
    'https://services.arcgis.com/v01gqwM5QqNysAAi/arcgis/rest/services/'
    'National_Earth_Surface_v2/FeatureServer')
CNGM_GEOLOGY = f'{CNGM_SERVICE}/6'
CNGM_FAULTS = f'{CNGM_SERVICE}/1'
CNGM_DATA_SOURCES = f'{CNGM_SERVICE}/13'
CNGM_MAP_SOURCE_ID = 'map50'
CNGM_WHERE = "MapSourceID='map50'"
CNGM_MAP_SOURCE = {
    'OBJECTID': 1583,
    'Source': ('Tweto, Ogden, 1979, Geologic map of Colorado: '
               'U.S. Geological Survey, scale 1:500,000.'),
    'Notes': None,
    'URL': None,
    'DataSources_ID': CNGM_MAP_SOURCE_ID,
}
CNGM_MAP_SOURCE_SHA256 = (
    '0f63f7d66376835698c1c8d88c269bdaeef4aaa0329ba2bb7c5604df84464a9e')
CNGM_DATA_SOURCE_ID = '1035'
CNGM_DATA_SOURCE = {
    'OBJECTID': 1043,
    'Source': ('Tweto, Ogden, 1979, Geologic map of Colorado: '
               'U.S. Geological Survey, scale 1:500,000'),
    'Notes': None,
    'URL': 'https://ngmdb.usgs.gov/Prodesc/proddesc_68589.htm',
    'DataSources_ID': CNGM_DATA_SOURCE_ID,
}
CNGM_DATA_SOURCE_SHA256 = (
    'b4215944c3e221297594dbbf13ad6ff0cf3d0b8e7ca74d8844861050cc41f83d')
CNGM_ITEM_CONTRACT = {
    'id': CNGM_ITEM_ID,
    'title': "Cooperative National Geologic Map: Earth's Surface geology",
    'type': 'Feature Service',
    'owner': 'drsoller@usgs.gov_USGS',
    'url': CNGM_SERVICE,
    'access': 'public',
}

CGS_WEBMAP_ID = '04f86e4c09cc426eb5408a2e67f0aaa9'
CGS_WEBMAP_ITEM_URL = (
    'https://cologeosurvey.maps.arcgis.com/sharing/rest/content/items/'
    f'{CGS_WEBMAP_ID}')
CGS_WEBMAP_CONTRACT = {
    'id': CGS_WEBMAP_ID,
    'title': 'Fault Server',
    'type': 'Web Map',
    'owner': 'cgsgeodata',
    'url': None,
    'access': 'public',
}
CGS_FAULT_SERVICE = (
    'https://cgsarcimage.mines.edu/arcgis/rest/services/cgs_services/'
    'Fault_Server/MapServer')
CGS_QUATERNARY = f'{CGS_FAULT_SERVICE}/9'
CGS_CENOZOIC = f'{CGS_FAULT_SERVICE}/12'
CGS_ON006_CATALOG = (
    'https://coloradogeologicalsurvey.org/publications/'
    'colorado-earthquake-fault-map/')

DISTRICT_URL = (
    'https://coloradogeologicalsurvey.org/Docs/Pubs/'
    'ON-007-08D-v20201112.zip')
DISTRICT_CATALOG = (
    'https://coloradogeologicalsurvey.org/publications/'
    'historic-metal-mining-districts-colorado-data/')
DISTRICT_BYTES = 202_712_855
DISTRICT_SHA256 = (
    'cd2234141333df794c48e8fb55096c12bfa6067fc9fc75b7c9fd5672ee77afe4')
DISTRICT_PREFIX = 'ON-007-08D-GIS_Data-v20201112/'
DISTRICT_BASENAME = 'Colorado_Historic_Metal_Mining_Districts'
DISTRICT_MEMBER_CONTRACT = {
    f'{DISTRICT_PREFIX}{DISTRICT_BASENAME}.cpg': (
        5, '3ad3031f5503a4404af825262ee8232cc04d4ea6683d42c5dd0a2f2a27ac9824'),
    f'{DISTRICT_PREFIX}{DISTRICT_BASENAME}.dbf': (
        464_805,
        '2b9034555a7b3c3c0002c52a31a78a2915ae0580feeb403d747b6f1c308bc7b7'),
    f'{DISTRICT_PREFIX}{DISTRICT_BASENAME}.prj': (
        425, 'a5c36df1e7e680f4616e1dd3658d310813d31048d5fa08dcbaadcc9f129b0ff7'),
    f'{DISTRICT_PREFIX}{DISTRICT_BASENAME}.shp': (
        2_371_280,
        '525a8b25f4f6bf952a5bffd1cf861153c75c1c0959fe3d129bc4fd68e7808d2d'),
    f'{DISTRICT_PREFIX}{DISTRICT_BASENAME}.shp.xml': (
        46_744,
        '8c4ec5f554e995c00c3242624f317449ddac28af115623c22edd1b1cd7ac7182'),
    f'{DISTRICT_PREFIX}{DISTRICT_BASENAME}.shx': (
        3_164,
        'd7a07fe2c0fb648fb7e5f5f942ae8115c6b73664ae592e3653b06289df1d6e6c'),
}
DISTRICT_ARCHIVE_INVENTORY = [
    {'name': 'ON-007-08-Read_Me.pdf', 'bytes': 135_579,
     'crc32': 'b6c911c7', 'compressed_bytes': 123_126, 'is_dir': False},
    {'name': '__MACOSX/', 'bytes': 0, 'crc32': '00000000',
     'compressed_bytes': 0, 'is_dir': True},
    {'name': '__MACOSX/._ON-007-08-Read_Me.pdf', 'bytes': 266,
     'crc32': '041e0a6c', 'compressed_bytes': 151, 'is_dir': False},
    {'name': DISTRICT_PREFIX, 'bytes': 0, 'crc32': '00000000',
     'compressed_bytes': 0, 'is_dir': True},
    {'name': f'{DISTRICT_PREFIX}{DISTRICT_BASENAME}.cpg', 'bytes': 5,
     'crc32': '0e813c50', 'compressed_bytes': 7, 'is_dir': False},
    {'name': f'{DISTRICT_PREFIX}{DISTRICT_BASENAME}.dbf', 'bytes': 464_805,
     'crc32': '7bb59380', 'compressed_bytes': 8_476, 'is_dir': False},
    {'name': f'{DISTRICT_PREFIX}{DISTRICT_BASENAME}.prj', 'bytes': 425,
     'crc32': '5bb1687f', 'compressed_bytes': 268, 'is_dir': False},
    {'name': f'{DISTRICT_PREFIX}{DISTRICT_BASENAME}.sbn', 'bytes': 3_900,
     'crc32': '265fdb3c', 'compressed_bytes': 2_632, 'is_dir': False},
    {'name': f'{DISTRICT_PREFIX}{DISTRICT_BASENAME}.sbx', 'bytes': 332,
     'crc32': 'fad8df56', 'compressed_bytes': 204, 'is_dir': False},
    {'name': f'{DISTRICT_PREFIX}{DISTRICT_BASENAME}.shp', 'bytes': 2_371_280,
     'crc32': '4b75643e', 'compressed_bytes': 1_304_203, 'is_dir': False},
    {'name': f'{DISTRICT_PREFIX}{DISTRICT_BASENAME}.shp.xml', 'bytes': 46_744,
     'crc32': '10751670', 'compressed_bytes': 6_046, 'is_dir': False},
    {'name': f'{DISTRICT_PREFIX}{DISTRICT_BASENAME}.shx', 'bytes': 3_164,
     'crc32': 'a1d95d7c', 'compressed_bytes': 2_121, 'is_dir': False},
    {'name': 'ON-007-08D-rpt-v20201112.pdf', 'bytes': 204_315_967,
     'crc32': 'dde0270e', 'compressed_bytes': 201_262_322,
     'is_dir': False},
    {'name': '__MACOSX/._ON-007-08D-rpt-v20201112.pdf', 'bytes': 210,
     'crc32': '940fc2f8', 'compressed_bytes': 119, 'is_dir': False},
]
DISTRICT_ARCHIVE_INVENTORY_SHA256 = (
    '6b494bf6eb14f8b8360f21fa52e8bce2739a1d850898338e6e87fa417b5ecfcf')
DISTRICT_SCHEMA = {
    'properties': {
        'Source': 'str:150', 'District': 'str:254', 'WebPage': 'str:254',
        'County_1': 'str:150', 'County_2': 'str:150', 'Note': 'str:254',
    },
    'geometry': 'Polygon',
}
DISTRICT_NATIVE_BOUNDS = (
    226446.34040592395, 4102227.912323936,
    676912.45885984, 4538592.5179551)

MI16_PDF_URL = 'https://coloradogeologicalsurvey.org/Docs/Pubs/MI-16.pdf'
MI16_PDF_BYTES = 179_398_577
SGMC_2026_DOWNLOAD_BYTES = 1_818_900_910

CO_BOUNDS = (-109.0602, 36.99245, -102.04209, 41.00344)
TIPPECANOE_VERSION = 'v2.79.0'
TIPPECANOE_MAXZOOM = 12
TIPPECANOE_FULL_DETAIL = 14
USER_AGENT = (
    'nw-mineral-monitor/11 Colorado state-survey baseline builder '
    '(official public research data)')

# Tippecanoe defaults the archive name to the output filename.  That leaks a
# temporary/path-dependent value into PMTiles JSON metadata.  Supply the exact
# logical manifest key so two builds at unrelated private paths remain equal.
ARCHIVE_NAMES = {
    'usgs-cngm-tweto-500k.pmtiles': 'co_usgs_cngm_tweto_500k',
    'cgs-on006-faults.pmtiles': 'co_cgs_on006_faults',
    'cgs-on007-districts.pmtiles': 'co_cgs_on007_districts',
}
ARCHIVE_ATTRIBUTIONS = {
    'usgs-cngm-tweto-500k.pmtiles':
        'USGS CNGM map50; Tweto 1979; Colorado Geological Survey MI-16',
    'cgs-on006-faults.pmtiles':
        'Colorado Geological Survey ON-006-15M',
    'cgs-on007-districts.pmtiles':
        'Colorado Geological Survey ON-007-08D v20201112',
}

COMMON_PROVENANCE = (
    'fid', 'st', 'source_dataset', 'source_id', 'source_scale',
    'source_scale_status', 'source_ref', 'source_url', 'publication_id')
LAYER_REQUIREMENTS = {
    'co_cngm_tweto_geology': [
        *COMMON_PROVENANCE, 'MapSourceID', 'DataSourceID', 'map_source_id',
        'data_source_id', 'source_map_citation', 'data_source_citation'],
    'co_cngm_tweto_faults': [
        *COMMON_PROVENANCE, 'MapSourceID', 'DataSourceID', 'map_source_id',
        'data_source_id', 'source_map_citation', 'data_source_citation'],
    'co_cgs_on006_quaternary_faults': [
        *COMMON_PROVENANCE, 'fault_age_scope'],
    'co_cgs_on006_cenozoic_faults': [
        *COMMON_PROVENANCE, 'fault_age_scope'],
    'co_cgs_on007_districts': [
        *COMMON_PROVENANCE, 'district_name', 'boundary_status'],
}

BROWSER_LAYER_CONTRACTS = {
    'co_cngm_tweto_geology': {
        'title': 'Colorado geology — Tweto (1979), 1:500,000',
        'geometry': 'polygon', 'activation_zoom': 4,
        'style': {
            'type': 'fill',
            'paint': {'fill-color': '#b69b72', 'fill-opacity': 0.22,
                      'fill-outline-color': '#6f604d'},
        },
        'semantic_note': 'Statewide bedrock/surficial map-unit polygons.',
    },
    'co_cngm_tweto_faults': {
        'title': 'Colorado structures — Tweto (1979), 1:500,000',
        'geometry': 'line', 'activation_zoom': 5,
        'style': {
            'type': 'line',
            'paint': {
                'line-color': '#4a3b32', 'line-opacity': 0.78,
                'line-width': ['interpolate', ['linear'], ['zoom'],
                               5, 0.65, 12, 1.8],
            },
        },
        'semantic_note': 'Tweto map structures; not an activity classification.',
    },
    'co_cgs_on006_quaternary_faults': {
        'title': 'CGS ON-006 Quaternary faults',
        'geometry': 'line', 'activation_zoom': 5,
        'style': {
            'type': 'line',
            'paint': {
                'line-color': '#d94841', 'line-opacity': 0.88,
                'line-width': ['interpolate', ['linear'], ['zoom'],
                               5, 0.8, 12, 2.2],
            },
        },
        'semantic_note': (
            'Exact Quaternary service layer; age scope is not a claim of '
            'current activity.'),
    },
    'co_cgs_on006_cenozoic_faults': {
        'title': 'CGS ON-006 Cenozoic faults',
        'geometry': 'line', 'activation_zoom': 6,
        'style': {
            'type': 'line',
            'paint': {
                'line-color': '#b86b28', 'line-opacity': 0.72,
                'line-width': ['interpolate', ['linear'], ['zoom'],
                               6, 0.7, 12, 1.8],
                'line-dasharray': [2, 2],
            },
        },
        'semantic_note': (
            'Exact Cenozoic service layer, separate from Quaternary faults; '
            'age scope is not an activity classification.'),
    },
    'co_cgs_on007_districts': {
        'title': 'Historic metal-mining districts — CGS ON-007-08D',
        'geometry': 'polygon', 'activation_zoom': 5,
        'style': {
            'type': 'fill',
            'paint': {'fill-color': '#d19a37', 'fill-opacity': 0.14,
                      'fill-outline-color': '#8a5b16'},
        },
        'semantic_note': (
            'Estimated, subjective historic district footprints; not tenure '
            'or a mineral-resource boundary.'),
    },
}

SOURCE_SPECS = {
    'cngm_geology': {
        'url': CNGM_GEOLOGY, 'where': CNGM_WHERE,
        'name': 'Map Units', 'geometry_type': 'esriGeometryPolygon',
        'kind': 'polygon', 'layer': 'co_cngm_tweto_geology',
        'fields': (
            'OBJECTID', 'MapUnit', 'IdentityConfidence', 'Label', 'Symbol',
            'DataSourceID', 'Notes', 'MapUnitPolys_ID', 'MapSourceID',
            'Source_MapUnit'),
    },
    'cngm_faults': {
        'url': CNGM_FAULTS, 'where': CNGM_WHERE,
        'name': 'Faults only', 'geometry_type': 'esriGeometryPolyline',
        'kind': 'line', 'layer': 'co_cngm_tweto_faults',
        'fields': (
            'OBJECTID', 'Type', 'IsConcealed', 'LocationConfidenceMeters',
            'ExistenceConfidence', 'IdentityConfidence', 'Label', 'Symbol',
            'DataSourceID', 'Notes', 'ContactsAndFaults_ID', 'MapSourceID'),
    },
    'cgs_quaternary': {
        'url': CGS_QUATERNARY, 'where': '1=1',
        'name': 'Quaternary Faults', 'geometry_type': 'esriGeometryPolyline',
        'kind': 'line', 'layer': 'co_cgs_on006_quaternary_faults',
        'fields': (
            'OBJECTID_1', 'OBJECTID', 'NAME', 'NUM', 'CODE', 'SLIP',
            'ALPHA_ID', 'TYPE', 'PALEOEVENT', 'SHORT_NAME'),
    },
    'cgs_cenozoic': {
        'url': CGS_CENOZOIC, 'where': '1=1',
        'name': 'Cenozoic faults', 'geometry_type': 'esriGeometryPolyline',
        'kind': 'line', 'layer': 'co_cgs_on006_cenozoic_faults',
        'fields': (
            'OBJECTID_1', 'OBJECTID', 'NAME', 'NUM', 'CODE', 'SLIP',
            'ALPHA_ID', 'TYPE', 'PALEOEVENT', 'SHORT_NAME'),
    },
}

ARCGIS_SNAPSHOT_CONTRACTS = {
    'cngm_geology': {
        'object_id_field': 'OBJECTID', 'n': 9_500,
        'minimum_object_id': 13_376, 'maximum_object_id': 435_296,
        'object_ids_sha256':
            '66089b8fecc05b9a7a468df2cff4be13e57d29cb7bbcf2ebac4ad46a8cf51f9b',
        'layer_metadata_sha256':
            '60451dba7e6af11321fc9b17f7d8cd571299c44a2f72548e62c980a74f246a8e',
    },
    'cngm_faults': {
        'object_id_field': 'OBJECTID', 'n': 10_238,
        'minimum_object_id': 2_444, 'maximum_object_id': 1_166_090,
        'object_ids_sha256':
            '68710305751927086394e1843a6dca4bb125fd0bcfe507e831fb6dd0e971cad6',
        'layer_metadata_sha256':
            '3b7e4611647fdd5bad09098034c3241f29876fb8dd62f6f402a69587e28d7815',
    },
    'cgs_quaternary': {
        'object_id_field': 'OBJECTID_1', 'n': 864,
        'minimum_object_id': 1, 'maximum_object_id': 864,
        'object_ids_sha256':
            '65f62eb61e7db1df05ae985cdc3f2a868ea3597822af3a50e01eda614a25cf58',
        'layer_metadata_sha256':
            '2f212505b8228950519ea647f28f8302b6b55380cd8e61a6769772baf58bc03e',
    },
    'cgs_cenozoic': {
        'object_id_field': 'OBJECTID_1', 'n': 2_698,
        'minimum_object_id': 1, 'maximum_object_id': 2_698,
        'object_ids_sha256':
            '646c20c880efeea52d09ad0df7887e0bf670fc32fc2c58727d4bed2fb04083ed',
        'layer_metadata_sha256':
            '23d4ebff121b4513be0a464743170c8291e3f1713f90d8665ed7e9f7e9d04c89',
    },
}

EMPTY_SHA256 = (
    '4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945')
GEOMETRY_CONTRACTS = {
    'cngm_geology': {
        'source_records': 9_500,
        'source_geometry_types': {'Polygon': 9_500},
        'tiled_geometry_types': {'Polygon': 9_500},
        'empty_count': 0, 'empty_sha256': EMPTY_SHA256,
        'fully_outside_count': 0, 'fully_outside_sha256': EMPTY_SHA256,
        'clipped_count': 158,
        'clipped_sha256':
            '952f59f1948b99d7015c3e421d3011082fecc01d03ac255635099eb2de54980f',
        'repair_count': 17,
        'repair_ids_sha256':
            '4598f6494bb74df7d4bf3518d8ee82ea58fe60c5fb9e8f92092e6b95d89471b9',
        'repair_reason_counts': {'Ring Self-intersection': 17},
        'repair_transition_counts': {'Polygon->Polygon->Polygon': 17},
        'max_absolute_area_delta': 1e-12,
        'max_relative_area_delta': 1e-12,
    },
    'cngm_faults': {
        'source_records': 10_238,
        'source_geometry_types': {'LineString': 10_238},
        'tiled_geometry_types': {'LineString': 10_238},
        'empty_count': 0, 'empty_sha256': EMPTY_SHA256,
        'fully_outside_count': 0, 'fully_outside_sha256': EMPTY_SHA256,
        'clipped_count': 13,
        'clipped_sha256':
            'eb6f805a95e0ed90189c102406572f9e653470ca0654e287940c03ccf1dd9ffa',
        'repair_count': 0, 'repair_ids_sha256': EMPTY_SHA256,
        'repair_reason_counts': {}, 'repair_transition_counts': {},
        'max_absolute_area_delta': 0.0, 'max_relative_area_delta': 0.0,
    },
    'cgs_quaternary': {
        'source_records': 864,
        'source_geometry_types': {'LineString': 864},
        'tiled_geometry_types': {'LineString': 864},
        'empty_count': 0, 'empty_sha256': EMPTY_SHA256,
        'fully_outside_count': 0, 'fully_outside_sha256': EMPTY_SHA256,
        'clipped_count': 2,
        'clipped_sha256':
            '11cf2dac157e7f01d66a4279dd44f61df855cd2904d71dcf7758a179093b2b1f',
        'repair_count': 0, 'repair_ids_sha256': EMPTY_SHA256,
        'repair_reason_counts': {}, 'repair_transition_counts': {},
        'max_absolute_area_delta': 0.0, 'max_relative_area_delta': 0.0,
    },
    'cgs_cenozoic': {
        'source_records': 2_698,
        'source_geometry_types': {'LineString': 2_698},
        'tiled_geometry_types': {'LineString': 2_698},
        'empty_count': 0, 'empty_sha256': EMPTY_SHA256,
        'fully_outside_count': 0, 'fully_outside_sha256': EMPTY_SHA256,
        'clipped_count': 4,
        'clipped_sha256':
            'd63a3bbc5f22b55a2da7b41f90c1a27f6d790286ccdcc352708f931cf0a082e5',
        'repair_count': 0, 'repair_ids_sha256': EMPTY_SHA256,
        'repair_reason_counts': {}, 'repair_transition_counts': {},
        'max_absolute_area_delta': 0.0, 'max_relative_area_delta': 0.0,
    },
    'districts': {
        'source_records': 383,
        'source_geometry_types': {'Polygon': 382, 'MultiPolygon': 1},
        'tiled_geometry_types': {'Polygon': 382, 'MultiPolygon': 1},
        'empty_count': 0, 'empty_sha256': EMPTY_SHA256,
        'fully_outside_count': 0, 'fully_outside_sha256': EMPTY_SHA256,
        'clipped_count': 1,
        'clipped_sha256':
            '4966768f01a22b7f23d1788dc9da8f9b3012d49ea3a1c6a653426e9397ad663f',
        'repair_count': 1,
        'repair_ids_sha256':
            '46b1884167c4edd308bcf0c04163dd02d05c9742b35e86b57b5f7ed1b82f3850',
        'repair_reason_counts': {'Ring Self-intersection': 1},
        'repair_transition_counts': {'Polygon->Polygon->Polygon': 1},
        'max_absolute_area_delta': 1e-5,
        'max_relative_area_delta': 1e-12,
    },
}

# Filled only after a complete private two-pass build.  Publication is refused
# while any value remains null, so a same-ID in-place source mutation cannot be
# accepted merely because counts and ArcGIS schemas still match.
SOURCE_SEQUENCE_SHA256 = {
    'cngm_geology':
        '846e4c65ccae2a92cc0c5ccf6dc406561623002a466bc17bb7c98aaab552c23b',
    'cngm_faults':
        '515fa0a2c73aac66af0c6a6f394bd399c5a82f361c8ce1fab3df4458465897c1',
    'cgs_quaternary':
        'da4a8eec6b69cb0a788e471d00d279a18e70028233f233c0ac1e455f5c25733b',
    'cgs_cenozoic':
        '2df3be6a984a34d6976749112b8a59a25f47ac1170d18deca159b745657f4777',
    'districts':
        '902a182cf18ce029d2989ee2cf2e9e1e94c54a9d6b52e3846297eadce594691f',
}


def _text(value, limit=1000):
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
    site = os.path.realpath(SITE)
    staging = os.path.realpath(PRIVATE_STAGING_ROOT)
    try:
        inside_site = os.path.commonpath((site, staging)) == site
    except ValueError as exc:
        raise RuntimeError('Colorado staging path is not resolvable') from exc
    if inside_site:
        raise RuntimeError('Colorado staging root must be outside public site/')
    os.makedirs(staging, exist_ok=True)
    return staging


def _positive_oid(value, field='OBJECTID'):
    if isinstance(value, bool):
        raise RuntimeError(f'{field} is boolean')
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f'{field} is not an integer: {value!r}') from exc
    if result <= 0 or (isinstance(value, float) and value != result):
        raise RuntimeError(f'{field} is not a positive integer: {value!r}')
    return result


def _request_json(url, params=None, *, post=False, tries=6):
    params = dict(params or {})
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
            with urllib.request.urlopen(request, timeout=240) as response:
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
                    f'official source request failed: HTTP {exc.code} ({url})'
                ) from exc
        except (urllib.error.URLError, TimeoutError,
                json.JSONDecodeError) as exc:
            last = exc
        if attempt + 1 < tries:
            time.sleep(min(30, 2 ** attempt))
    raise RuntimeError(f'official source request failed after {tries} tries: {last}')


def _selected_item(item):
    return {field: item.get(field) for field in (
        'id', 'title', 'type', 'owner', 'url', 'access')}


def _verify_authority_items():
    cngm = _selected_item(_request_json(CNGM_ITEM_URL, {'f': 'json'}))
    cgs = _selected_item(_request_json(CGS_WEBMAP_ITEM_URL, {'f': 'json'}))
    if cngm != CNGM_ITEM_CONTRACT:
        raise RuntimeError(f'CNGM ArcGIS item identity changed: {cngm}')
    if cgs != CGS_WEBMAP_CONTRACT:
        raise RuntimeError(f'CGS ON-006 web-map identity changed: {cgs}')
    return {'cngm': cngm, 'cgs_on006': cgs}


def _selected_layer_metadata(metadata):
    return {
        'name': metadata.get('name'),
        'geometryType': metadata.get('geometryType'),
        'description': metadata.get('description'),
        'copyrightText': metadata.get('copyrightText'),
        'maxRecordCount': metadata.get('maxRecordCount'),
        'fields': [
            {field: item.get(field) for field in (
                'name', 'alias', 'type', 'length')}
            for item in metadata.get('fields') or []
            if isinstance(item, dict)
        ],
    }


def _layer_snapshot(key):
    spec = SOURCE_SPECS[key]
    metadata = _request_json(spec['url'], {'f': 'json'})
    selected = _selected_layer_metadata(metadata)
    if (selected['name'] != spec['name'] or
            selected['geometryType'] != spec['geometry_type']):
        raise RuntimeError(f'{key} official layer identity changed')
    oid_fields = [
        item['name'] for item in selected['fields']
        if item.get('type') == 'esriFieldTypeOID']
    ids_result = _request_json(f"{spec['url']}/query", {
        'f': 'json', 'where': spec['where'], 'returnIdsOnly': 'true'})
    oid_field = ids_result.get('objectIdFieldName')
    raw_ids = ids_result.get('objectIds')
    if (not isinstance(oid_field, str) or oid_fields != [oid_field] or
            not isinstance(raw_ids, list)):
        raise RuntimeError(f'{key} typed object-ID contract changed')
    ids = sorted(_positive_oid(value, oid_field) for value in raw_ids)
    if not ids or len(ids) != len(set(ids)):
        raise RuntimeError(f'{key} returned empty or duplicate object IDs')
    snapshot = {
        'oid_field': oid_field, 'ids': ids,
        'object_ids_sha256': _canonical_sha256(ids),
        'layer_metadata_sha256': _canonical_sha256(selected),
        'metadata': selected,
    }
    _assert_snapshot_contract(key, snapshot)
    return snapshot


def _snapshot_manifest(snapshot):
    ids = snapshot['ids']
    return {
        'object_id_field': snapshot['oid_field'], 'n': len(ids),
        'minimum_object_id': ids[0], 'maximum_object_id': ids[-1],
        'object_ids_sha256': snapshot['object_ids_sha256'],
        'layer_metadata_sha256': snapshot['layer_metadata_sha256'],
    }


def _assert_snapshot_contract(key, snapshot):
    ids = snapshot.get('ids') or []
    observed = {
        'object_id_field': snapshot.get('oid_field'), 'n': len(ids),
        'minimum_object_id': ids[0] if ids else None,
        'maximum_object_id': ids[-1] if ids else None,
        'object_ids_sha256': snapshot.get('object_ids_sha256'),
        'layer_metadata_sha256': snapshot.get('layer_metadata_sha256'),
    }
    if observed != ARCGIS_SNAPSHOT_CONTRACTS[key]:
        raise RuntimeError(
            f'{key} ArcGIS snapshot changed; review required: {observed}')


def _verify_cngm_source_bindings():
    result = {}
    for label, source_id, expected, expected_sha in (
            ('map_source', CNGM_MAP_SOURCE_ID, CNGM_MAP_SOURCE,
             CNGM_MAP_SOURCE_SHA256),
            ('data_source', CNGM_DATA_SOURCE_ID, CNGM_DATA_SOURCE,
             CNGM_DATA_SOURCE_SHA256)):
        response = _request_json(f'{CNGM_DATA_SOURCES}/query', {
            'f': 'json', 'where': f"DataSources_ID='{source_id}'",
            'outFields': 'OBJECTID,Source,Notes,URL,DataSources_ID',
            'returnGeometry': 'false', 'orderByFields': 'OBJECTID ASC'})
        rows = [feature.get('attributes')
                for feature in response.get('features') or []]
        if rows != [expected] or _canonical_sha256(rows) != expected_sha:
            raise RuntimeError(
                f'CNGM {label} {source_id} binding changed: {rows}')
        result[label] = rows[0]
    return result


def _iter_snapshot(key, snapshot, page=250):
    spec = SOURCE_SPECS[key]
    oid_field, ids = snapshot['oid_field'], snapshot['ids']
    emitted = 0
    for start in range(0, len(ids), page):
        expected = ids[start:start + page]
        response = _request_json(f"{spec['url']}/query", {
            'f': 'geojson', 'objectIds': ','.join(map(str, expected)),
            'outFields': ','.join(dict.fromkeys((oid_field, *spec['fields']))),
            'returnGeometry': 'true', 'returnTrueCurves': 'false',
            'outSR': 4326, 'geometryPrecision': 8,
            'orderByFields': f'{oid_field} ASC',
        }, post=True)
        features = response.get('features')
        if not isinstance(features, list):
            raise RuntimeError(f'{key} snapshot page has no feature array')
        actual = [
            _positive_oid((feature.get('properties') or {}).get(oid_field),
                          oid_field)
            for feature in features]
        if actual != expected:
            raise RuntimeError(
                f'{key} page {start} does not match pinned IDs: '
                f'expected={expected[:3]}..{expected[-3:]}, '
                f'actual={actual[:3]}..{actual[-3:]}')
        yield from features
        emitted += len(features)
        if emitted % 5_000 < page or emitted == len(ids):
            print(f'{key}: {emitted:,}/{len(ids):,}')
    if emitted != len(ids):
        raise RuntimeError(f'{key} emitted {emitted}; expected {len(ids)}')


def _load_co_clip():
    if shapely_shape is None or shapely_prepare is None:
        raise RuntimeError('Shapely 2.x is required for Colorado clipping')
    with open(STATE_CLIPS, encoding='utf-8') as source:
        document = json.load(source)
    if (document.get('schema_version') != 1 or
            set(document.get('states') or {}) != {
                code for code in (document.get('states') or {})} or
            len(document.get('states') or {}) != 49 or
            'TIGERweb' not in str(document.get('source') or '')):
        raise RuntimeError('authoritative state clip index is invalid')
    boundary = shapely_shape(document['states'].get('CO'))
    if (boundary.geom_type != 'Polygon' or boundary.is_empty or
            not boundary.is_valid or tuple(boundary.bounds) != CO_BOUNDS):
        raise RuntimeError('authoritative Colorado boundary is invalid')
    return {
        'boundary': boundary, 'prepared': shapely_prepare(boundary),
        'manifest': {
            'artifact': os.path.relpath(STATE_CLIPS, ROOT),
            'artifact_sha256': _sha256(STATE_CLIPS),
            'authority': document['source'],
            'method': 'geometric intersection',
        },
    }


def _atomic_parts(geometry):
    if geometry.geom_type in ('Polygon', 'LineString'):
        return [geometry]
    if geometry.geom_type in ('MultiPolygon', 'MultiLineString',
                              'GeometryCollection'):
        result = []
        for part in geometry.geoms:
            result.extend(_atomic_parts(part))
        return result
    return []


def _same_dimension(geometry, kind):
    wanted = 'Polygon' if kind == 'polygon' else 'LineString'
    parts = [part for part in _atomic_parts(geometry)
             if part.geom_type == wanted and not part.is_empty and
             (part.area > 0 if kind == 'polygon' else part.length > 0)]
    if not parts:
        return None
    result = shapely_unary_union(parts)
    allowed = (('Polygon', 'MultiPolygon') if kind == 'polygon'
               else ('LineString', 'MultiLineString'))
    if result.is_empty or result.geom_type not in allowed or not result.is_valid:
        raise RuntimeError(
            f'{kind} dimensional extraction produced {result.geom_type!r}')
    return result


def _repair_polygon(geometry, oid):
    if geometry.is_valid:
        return geometry, None
    reason = shapely_explain_validity(geometry)
    repaired = shapely_make_valid(geometry)
    output = _same_dimension(repaired, 'polygon')
    if output is None:
        raise RuntimeError(f'polygon {oid} repair has no polygonal output')
    source_area = float(geometry.area)
    repaired_area = float(output.area)
    absolute = abs(repaired_area - source_area)
    relative = absolute / source_area if source_area else math.inf
    return output, {
        'object_id': oid,
        'validity_reason': reason,
        'validity_reason_class': reason.split('[', 1)[0],
        'source_type': geometry.geom_type,
        'make_valid_type': repaired.geom_type,
        'polygon_output_type': output.geom_type,
        'source_area': source_area, 'repaired_area': repaired_area,
        'absolute_area_delta': absolute, 'relative_area_delta': relative,
    }


def _clip_shape(geometry, kind, clip):
    changed = not clip['prepared'].covers(geometry)
    result = geometry.intersection(clip['boundary']) if changed else geometry
    output = _same_dimension(result, kind)
    return output, changed


def _base_properties(fid, *, dataset, source_id, scale, scale_status,
                     source_ref, source_url, publication_id):
    return {
        'fid': fid, 'st': 'CO', 'source_dataset': dataset,
        'source_id': source_id, 'source_scale': scale,
        'source_scale_status': scale_status, 'source_ref': source_ref,
        'source_url': source_url, 'publication_id': publication_id,
    }


def _normalize_arcgis(key, raw, oid, geometry):
    properties = raw.get('properties') or {}
    if key.startswith('cngm_'):
        if properties.get('MapSourceID') != CNGM_MAP_SOURCE_ID:
            raise RuntimeError(f'{key} feature {oid} escaped map50 filter')
        data_source = properties.get('DataSourceID')
        if data_source != CNGM_DATA_SOURCE_ID:
            raise RuntimeError(
                f'{key} feature {oid} has changed DataSourceID {data_source!r}')
        result = _base_properties(
            oid, dataset='usgs_cngm_earth_surface_tweto_map50',
            source_id=f'cngm-map50:{key}:{oid}', scale='1:500,000',
            scale_status='CNGM DataSources map50 citation',
            source_ref='CNGM DataSources map50 / Tweto (1979)',
            source_url='https://ngmdb.usgs.gov/Prodesc/proddesc_68589.htm',
            publication_id='Tweto 1979 / CGS MI-16 / CNGM map50')
        result.update({
            # Preserve the two source linkage fields literally as well as in
            # registry-friendly snake case.  Their exact DataSources table
            # rows are checksum-bound separately.
            'MapSourceID': CNGM_MAP_SOURCE_ID,
            'DataSourceID': data_source,
            'map_source_id': CNGM_MAP_SOURCE_ID,
            'data_source_id': data_source,
            'source_map_citation': CNGM_MAP_SOURCE['Source'],
            'data_source_citation': CNGM_DATA_SOURCE['Source'],
            'data_source_url': CNGM_DATA_SOURCE['URL'],
            'identity_confidence': _text(properties.get('IdentityConfidence'), 200),
            'label': _text(properties.get('Label'), 300),
            'symbol': _text(properties.get('Symbol'), 100),
            'notes': _text(properties.get('Notes'), 800),
        })
        if key == 'cngm_geology':
            result.update({
                'map_unit': _text(properties.get('MapUnit'), 300),
                'source_map_unit': _text(properties.get('Source_MapUnit'), 300),
                'map_unit_feature_id': _text(
                    properties.get('MapUnitPolys_ID'), 300),
            })
        else:
            result.update({
                'fault_type': _text(properties.get('Type'), 300),
                'is_concealed': _text(properties.get('IsConcealed'), 100),
                'location_confidence_m': properties.get(
                    'LocationConfidenceMeters'),
                'existence_confidence': _text(
                    properties.get('ExistenceConfidence'), 200),
                'fault_feature_id': _text(
                    properties.get('ContactsAndFaults_ID'), 300),
            })
    else:
        age_scope = ('Quaternary' if key == 'cgs_quaternary'
                     else 'Cenozoic')
        result = _base_properties(
            oid, dataset='cgs_on006_15m_fault_server',
            source_id=f'cgs-on006:{key}:{oid}',
            scale='variable (CGS online compilation)',
            scale_status='CGS ON-006-15M catalog metadata',
            source_ref=f'CGS ON-006-15M {age_scope} fault layer',
            source_url=CGS_ON006_CATALOG,
            publication_id='CGS ON-006-15M')
        result.update({
            'fault_age_scope': age_scope,
            'source_object_id': oid,
            'legacy_object_id': properties.get('OBJECTID'),
            'fault_name': _text(properties.get('NAME'), 300),
            'short_name': _text(properties.get('SHORT_NAME'), 300),
            'fault_number': _text(properties.get('NUM'), 100),
            'alpha_id': _text(properties.get('ALPHA_ID'), 100),
            'fault_type': _text(properties.get('TYPE'), 200),
            'slip': _text(properties.get('SLIP'), 100),
            'paleoevent': _text(properties.get('PALEOEVENT'), 800),
        })
    return {
        'type': 'Feature', 'id': oid, 'properties': result,
        'geometry': shapely_mapping(geometry),
    }


def _write_feature(output, feature):
    output.write(json.dumps(
        feature, sort_keys=True, separators=(',', ':'), allow_nan=False))
    output.write('\n')


def _repair_evidence(records, *, ordering, area_units):
    reasons = Counter(record['validity_reason_class'] for record in records)
    transitions = Counter(
        '->'.join((record['source_type'], record['make_valid_type'],
                  record['polygon_output_type']))
        for record in records)
    absolute = max(records, key=lambda row: row['absolute_area_delta']) \
        if records else None
    relative = max(records, key=lambda row: row['relative_area_delta']) \
        if records else None
    return {
        'status': ('reviewed_pinned_source_repair' if records else
                   'reviewed_pinned_source_no_repair_required'),
        'ordering': ordering,
        'method': ('GEOSMakeValid via shapely.make_valid' if records else None),
        'shapely_version': getattr(shapely, '__version__', None),
        'geos_version': getattr(shapely, 'geos_version_string', None),
        'count': len(records),
        'object_ids': [record['object_id'] for record in records],
        'object_ids_sha256': _canonical_sha256(
            [record['object_id'] for record in records]),
        'validity_reason_counts': dict(sorted(reasons.items())),
        'type_transition_counts': dict(sorted(transitions.items())),
        'records': records,
        'records_sha256': _canonical_sha256(records),
        'area_delta': {
            'units': area_units,
            'maximum_absolute': {
                'value': (absolute['absolute_area_delta'] if absolute else 0.0),
                'object_id': (absolute['object_id'] if absolute else None),
            },
            'maximum_relative': {
                'value': (relative['relative_area_delta'] if relative else 0.0),
                'object_id': (relative['object_id'] if relative else None),
            },
            'sum_absolute': sum(
                record['absolute_area_delta'] for record in records),
        },
    }


def _finalize_stream_stats(key, snapshot_ids, source_types, output_types,
                           empty, outside, clipped, repairs, sequence,
                           *, repair_ordering, area_units):
    evidence = _repair_evidence(
        repairs, ordering=repair_ordering, area_units=area_units)
    result = {
        'source_records': len(snapshot_ids),
        'n': len(snapshot_ids) - len(empty) - len(outside),
        'source_geometry_types': dict(sorted(source_types.items())),
        'tiled_geometry_types': dict(sorted(output_types.items())),
        'empty_geometry_count': len(empty),
        'empty_geometry_object_ids': empty,
        'empty_geometry_object_ids_sha256': _canonical_sha256(empty),
        'topology_repair': evidence,
        'spatial_clip': {
            'ordering': 'topology_repair_before_state_intersection',
            'fully_outside_count': len(outside),
            'fully_outside_object_ids': outside,
            'fully_outside_object_ids_sha256': _canonical_sha256(outside),
            'geometry_clipped_count': len(clipped),
            'geometry_clipped_object_ids': clipped,
            'geometry_clipped_object_ids_sha256': _canonical_sha256(clipped),
            'geometry_unchanged_count': (
                len(snapshot_ids) - len(empty) - len(outside) - len(clipped)),
        },
        'sequence_bytes': os.path.getsize(sequence),
        'sequence_sha256': _sha256(sequence),
    }
    _assert_geometry_contract(key, result)
    pinned = SOURCE_SEQUENCE_SHA256.get(key)
    if pinned is not None and result['sequence_sha256'] != pinned:
        raise RuntimeError(
            f'{key} normalized source content changed: '
            f'{result["sequence_sha256"]} != {pinned}')
    return result


def _assert_geometry_contract(key, stats):
    contract = GEOMETRY_CONTRACTS[key]
    repair = stats['topology_repair']
    clip = stats['spatial_clip']
    observed = {
        'source_records': stats['source_records'],
        'source_geometry_types': stats['source_geometry_types'],
        'tiled_geometry_types': stats['tiled_geometry_types'],
        'empty_count': stats['empty_geometry_count'],
        'empty_sha256': stats['empty_geometry_object_ids_sha256'],
        'fully_outside_count': clip['fully_outside_count'],
        'fully_outside_sha256': clip['fully_outside_object_ids_sha256'],
        'clipped_count': clip['geometry_clipped_count'],
        'clipped_sha256': clip['geometry_clipped_object_ids_sha256'],
        'repair_count': repair['count'],
        'repair_ids_sha256': repair['object_ids_sha256'],
        'repair_reason_counts': repair['validity_reason_counts'],
        'repair_transition_counts': repair['type_transition_counts'],
    }
    expected = {field: contract[field] for field in observed}
    if observed != expected:
        raise RuntimeError(
            f'{key} geometry/clip contract changed: '
            f'expected={expected}, observed={observed}')
    for field, ceiling in (
            ('maximum_absolute_area_delta',
             contract['max_absolute_area_delta']),
            ('maximum_relative_area_delta',
             contract['max_relative_area_delta'])):
        metric = ('maximum_absolute' if field.startswith('maximum_absolute')
                  else 'maximum_relative')
        value = repair['area_delta'][metric]['value']
        if not isinstance(value, (int, float)) or not math.isfinite(value) or \
                not 0 <= value <= ceiling:
            raise RuntimeError(
                f'{key} repair {field} {value} exceeds {ceiling}')


def _stream_arcgis(key, snapshot, sequence, clip):
    spec = SOURCE_SPECS[key]
    source_types = Counter()
    output_types = Counter()
    empty, outside, clipped, repairs = [], [], [], []
    with open(sequence, 'w', encoding='utf-8') as output:
        for raw in _iter_snapshot(key, snapshot):
            properties = raw.get('properties') or {}
            oid = _positive_oid(properties.get(snapshot['oid_field']),
                                snapshot['oid_field'])
            raw_geometry = raw.get('geometry')
            if not raw_geometry or not raw_geometry.get('coordinates'):
                empty.append(oid)
                continue
            geometry = shapely_shape(raw_geometry)
            if geometry.is_empty:
                empty.append(oid)
                continue
            source_types[geometry.geom_type] += 1
            repair = None
            if spec['kind'] == 'polygon':
                geometry, repair = _repair_polygon(geometry, oid)
                if repair is not None:
                    repairs.append(repair)
            elif not geometry.is_valid:
                raise RuntimeError(
                    f'{key} line {oid} is invalid: '
                    f'{shapely_explain_validity(geometry)}')
            geometry, changed = _clip_shape(geometry, spec['kind'], clip)
            if geometry is None:
                outside.append(oid)
                continue
            if changed:
                clipped.append(oid)
            output_types[geometry.geom_type] += 1
            _write_feature(output, _normalize_arcgis(
                key, raw, oid, geometry))
    return _finalize_stream_stats(
        key, snapshot['ids'], source_types, output_types, empty, outside,
        clipped, repairs, sequence,
        repair_ordering='validate_then_make_valid_in_epsg4326_then_state_intersection',
        area_units='square degrees in EPSG:4326')


def _download_district_zip(path):
    request = urllib.request.Request(
        DISTRICT_URL, headers={'User-Agent': USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=300) as response, \
                open(path, 'wb') as output:
            while True:
                block = response.read(1024 * 1024)
                if not block:
                    break
                output.write(block)
            output.flush()
            os.fsync(output.fileno())
    except (OSError, urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError(f'CGS district ZIP download failed: {exc}') from exc
    observed = {'bytes': os.path.getsize(path), 'sha256': _sha256(path)}
    expected = {'bytes': DISTRICT_BYTES, 'sha256': DISTRICT_SHA256}
    if observed != expected:
        raise RuntimeError(
            f'CGS district ZIP identity changed: {observed} != {expected}')
    return observed


def _extract_district_shapefile(archive_path, directory):
    output_directory = os.path.join(directory, 'district-source')
    os.makedirs(output_directory)
    with zipfile.ZipFile(archive_path) as archive:
        inventory = [
            {
                'name': info.filename, 'bytes': info.file_size,
                'crc32': f'{info.CRC:08x}',
                'compressed_bytes': info.compress_size,
                'is_dir': info.is_dir(),
            }
            for info in archive.infolist()
        ]
        if (inventory != DISTRICT_ARCHIVE_INVENTORY or
                _canonical_sha256(inventory) !=
                DISTRICT_ARCHIVE_INVENTORY_SHA256):
            raise RuntimeError('CGS district ZIP member inventory changed')
        infos = {info.filename: info for info in archive.infolist()}
        for member, (expected_bytes, expected_sha) in \
                DISTRICT_MEMBER_CONTRACT.items():
            info = infos[member]
            if (info.is_dir() or info.file_size != expected_bytes or
                    stat.S_ISLNK(info.external_attr >> 16)):
                raise RuntimeError(f'unsafe or changed ZIP member {member}')
            target = os.path.join(output_directory, os.path.basename(member))
            digest = hashlib.sha256()
            with archive.open(info) as source, open(target, 'wb') as output:
                for block in iter(lambda: source.read(1024 * 1024), b''):
                    digest.update(block)
                    output.write(block)
            if digest.hexdigest() != expected_sha:
                raise RuntimeError(f'ZIP member checksum changed: {member}')
    return os.path.join(output_directory, f'{DISTRICT_BASENAME}.shp')


def _district_feature(properties, fid, geometry):
    district_name = _text(properties.get('District'), 500)
    boundary_status = _text(properties.get('Note'), 500)
    if district_name is None or boundary_status is None:
        raise RuntimeError(f'CGS district row {fid} lacks name/boundary status')
    result = _base_properties(
        fid, dataset='cgs_on007_08d_historic_metal_mining_districts',
        source_id=f'cgs-on007-08d:{fid}', scale='1:150,000',
        scale_status='CGS ON-007-08D catalog metadata',
        source_ref='CGS ON-007-08D v20201112 source row '
                   f'{fid - 1}',
        source_url=DISTRICT_CATALOG,
        publication_id='CGS ON-007-08D v20201112')
    result.update({
        'district_name': district_name,
        'boundary_status': boundary_status,
        'county_1': _text(properties.get('County_1'), 200),
        'county_2': _text(properties.get('County_2'), 200),
        'district_report_url': _text(properties.get('WebPage'), 600),
        'source_citation': _text(properties.get('Source'), 800),
        'source_row': fid - 1,
        'boundary_is_estimated': 1,
    })
    return {'type': 'Feature', 'id': fid, 'properties': result,
            'geometry': shapely_mapping(geometry)}


def _stream_districts(shapefile, sequence, clip):
    if fiona is None or Transformer is None:
        raise RuntimeError('Fiona and pyproj are required for district ingestion')
    source_types = Counter()
    output_types = Counter()
    empty, outside, clipped, repairs, ids = [], [], [], [], []
    transformer = Transformer.from_crs(
        'EPSG:26913', 'EPSG:4326', always_xy=True).transform
    with fiona.open(shapefile) as source, \
            open(sequence, 'w', encoding='utf-8') as output:
        if (source.driver != 'ESRI Shapefile' or len(source) != 383 or
                source.crs.to_string() != 'EPSG:26913' or
                source.schema != DISTRICT_SCHEMA or
                tuple(source.bounds) != DISTRICT_NATIVE_BOUNDS):
            raise RuntimeError(
                'CGS district shapefile count/CRS/schema/bounds changed')
        for fid, raw in enumerate(source, 1):
            ids.append(fid)
            if raw.geometry is None:
                empty.append(fid)
                continue
            geometry = shapely_shape(raw.geometry)
            if geometry.is_empty:
                empty.append(fid)
                continue
            source_types[geometry.geom_type] += 1
            geometry, repair = _repair_polygon(geometry, fid)
            if repair is not None:
                repairs.append(repair)
            geometry = shapely_transform(transformer, geometry)
            geometry, changed = _clip_shape(geometry, 'polygon', clip)
            if geometry is None:
                outside.append(fid)
                continue
            if changed:
                clipped.append(fid)
            output_types[geometry.geom_type] += 1
            _write_feature(output, _district_feature(
                dict(raw.properties), fid, geometry))
    if ids != list(range(1, 384)):
        raise RuntimeError('CGS district source-row inventory changed')
    return _finalize_stream_stats(
        'districts', ids, source_types, output_types, empty, outside,
        clipped, repairs, sequence,
        repair_ordering=(
            'validate_then_make_valid_in_epsg26913_then_epsg4326_transform_'
            'then_state_intersection'),
        area_units='square meters in EPSG:26913')


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
        raise RuntimeError(f'unrecognized tippecanoe version: {output!r}')
    return match.group(1)


def _run_tippecanoe(output, layers, attribution):
    archive_name = ARCHIVE_NAMES.get(os.path.basename(output))
    build_directory = os.path.realpath(os.path.dirname(output))
    if (archive_name is None or
            ARCHIVE_ATTRIBUTIONS.get(os.path.basename(output)) != attribution or
            any(os.path.realpath(os.path.dirname(sequence)) != build_directory
                for _, sequence in layers)):
        raise RuntimeError(f'unregistered Colorado archive name: {output}')
    command = [
        # Relative basenames prevent Tippecanoe's generator_options metadata
        # from leaking a random private staging path.  Each independent build
        # receives identical local input names in its own directory.
        'tippecanoe', '--force', '--output', os.path.basename(output),
        f'--name={archive_name}', f'--description={archive_name}',
        '--minimum-zoom=0', f'--maximum-zoom={TIPPECANOE_MAXZOOM}',
        f'--full-detail={TIPPECANOE_FULL_DETAIL}',
        '--no-feature-limit', '--no-tile-size-limit',
        '--no-tiny-polygon-reduction-at-maximum-zoom',
        # Parallel reads can interleave named input layers and make otherwise
        # identical archives byte-distinct.  Determinism outranks throughput.
        '--simplify-only-low-zooms', '--quiet',
        f'--attribution={attribution}',
    ]
    for layer, sequence in layers:
        command.extend(('-L', f'{layer}:{os.path.basename(sequence)}'))
    subprocess.run(command, check=True, cwd=build_directory)


def _validate_pmtiles(path, layers, *, pmtiles_header=None):
    if pmtiles_header is None:
        from validate_national import _pmtiles_header as pmtiles_header
    requirements = {layer: LAYER_REQUIREMENTS[layer] for layer in layers}
    metadata = pmtiles_header(
        path, layers, requirements, verify_feature_properties=True,
        expected_state='CO', expected_bounds=[CO_BOUNDS],
        collect_feature_ids=True)
    if set(metadata['source_layers']) != set(layers):
        raise RuntimeError(
            f'{path} contains unexpected layers {metadata["source_layers"]}')
    if metadata['minzoom'] != 0 or metadata['maxzoom'] != TIPPECANOE_MAXZOOM:
        raise RuntimeError(f'{path} zoom contract changed')
    bounds = metadata['bounds']
    tolerance = 2e-6
    if (bounds[0] < CO_BOUNDS[0] - tolerance or
            bounds[1] < CO_BOUNDS[1] - tolerance or
            bounds[2] > CO_BOUNDS[2] + tolerance or
            bounds[3] > CO_BOUNDS[3] + tolerance):
        raise RuntimeError(f'{path} bounds escape Colorado: {bounds}')
    if any(metadata['semantic_layer_counts'].get(layer, 0) <= 0
           for layer in layers):
        raise RuntimeError(f'{path} contains an empty declared source layer')
    archive_key = ARCHIVE_NAMES.get(os.path.basename(path))
    if archive_key is None:
        raise RuntimeError(f'{path} has no Colorado archive identity')
    metadata['reproducible_metadata'] = _assert_path_independent_metadata(
        path, archive_key)
    return metadata


def _pmtiles_json_metadata(path):
    """Return strict decompressed metadata to audit path independence."""
    import gzip
    import struct
    import zlib

    with open(path, 'rb') as source:
        head = source.read(127)
        if len(head) != 127 or head[:7] != b'PMTiles' or head[7] != 3:
            raise RuntimeError(f'{path} has an invalid PMTiles header')
        metadata_offset, metadata_length = struct.unpack_from('<2Q', head, 24)
        internal_compression = head[97]
        source.seek(metadata_offset)
        payload = source.read(metadata_length)
    if internal_compression == 2:
        try:
            payload = gzip.decompress(payload)
        except OSError:
            payload = zlib.decompress(payload, 16 + zlib.MAX_WBITS)
    elif internal_compression != 1:
        raise RuntimeError(
            f'{path} has unsupported metadata compression '
            f'{internal_compression}')
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f'{path} has invalid JSON metadata: {exc}') from exc
    if not isinstance(value, dict):
        raise RuntimeError(f'{path} PMTiles metadata is not an object')
    return value


def _assert_path_independent_metadata(path, key):
    metadata = _pmtiles_json_metadata(path)
    options = metadata.get('generator_options')
    expected_attribution = ARCHIVE_ATTRIBUTIONS[os.path.basename(path)]
    if (metadata.get('name') != key or metadata.get('description') != key or
            metadata.get('attribution') != expected_attribution or
            metadata.get('generator') != f'tippecanoe {TIPPECANOE_VERSION}' or
            not isinstance(options, str) or '/' in options or '\\' in options):
        raise RuntimeError(
            f'{key} PMTiles metadata identity/options are not path-free')
    serialized = json.dumps(metadata, sort_keys=True, separators=(',', ':'))
    forbidden = (
        PRIVATE_STAGING_ROOT, os.path.dirname(path),
        'tile-set-a', 'tile-set-b', 'nwmm-co-baselines-',
    )
    leaks = [value for value in forbidden if value and value in serialized]
    if leaks:
        raise RuntimeError(
            f'{key} PMTiles metadata leaks private path/name values: {leaks}')
    return {
        'status': 'complete_path_free_reproducible_metadata',
        'name': metadata['name'],
        'metadata_sha256': _canonical_sha256(metadata),
        'generator_options_sha256': _canonical_sha256(options),
    }


def _expected_ids(source_key, snapshot, stats):
    ids = set(snapshot['ids']) if snapshot is not None else set(range(1, 384))
    ids -= set(stats['empty_geometry_object_ids'])
    ids -= set(stats['spatial_clip']['fully_outside_object_ids'])
    return ids


def _assert_unique_ids(source_key, layer, snapshot, stats, metadata):
    expected = _expected_ids(source_key, snapshot, stats)
    observed = set(metadata.get('maxzoom_feature_ids', {}).get(layer) or [])
    if observed != expected:
        raise RuntimeError(
            f'{layer} maxzoom IDs do not reconcile: '
            f'expected={len(expected)}, observed={len(observed)}, '
            f'missing={sorted(expected - observed)[:100]}, '
            f'extra={sorted(observed - expected)[:100]}')
    return {
        'status': 'complete',
        'source_records': stats['source_records'],
        'tileable_source_records': len(expected),
        'unique_maxzoom_ids': len(observed),
        'source_object_ids_sha256': _canonical_sha256(sorted(expected)),
        'maxzoom_object_ids_sha256': _canonical_sha256(sorted(observed)),
        'maxzoom_feature_instances': metadata.get(
            'maxzoom_feature_instances', {}).get(layer),
    }


def _artifact_fields(path, metadata):
    return {
        'bytes': os.path.getsize(path), 'sha256': _sha256(path),
        'bounds': metadata['bounds'],
        'minzoom': metadata['minzoom'], 'maxzoom': metadata['maxzoom'],
        'field_types': metadata['field_types'],
        'reproducible_metadata': metadata['reproducible_metadata'],
        'semantic_tile_feature_counts': metadata['semantic_layer_counts'],
    }


def _browser_descriptor(key, file, metadata, stats):
    source_by_layer = {
        'co_cngm_tweto_geology': 'cngm_geology',
        'co_cngm_tweto_faults': 'cngm_faults',
        'co_cgs_on006_quaternary_faults': 'cgs_quaternary',
        'co_cgs_on006_cenozoic_faults': 'cgs_cenozoic',
        'co_cgs_on007_districts': 'districts',
    }
    layers = []
    for layer in _artifact_layers()[key]:
        contract = BROWSER_LAYER_CONTRACTS[layer]
        layers.append({
            'layer_id': f'{layer}_baseline',
            'title': contract['title'],
            'source_layer': layer,
            'geometry': contract['geometry'],
            'style': json.loads(json.dumps(contract['style'])),
            'required_properties': list(LAYER_REQUIREMENTS[layer]),
            'feature_count': stats[source_by_layer[layer]]['n'],
            # The archive header is the range-request authority used by a
            # generic PMTiles client.  Per-layer activation uses the same
            # checksum-bound archive bounds without inventing coverage.
            'bounds': list(metadata['bounds']),
            'activation_zoom': contract['activation_zoom'],
            'default_visible': False,
            'semantic_note': contract['semantic_note'],
        })
    return {
        'schema_version': 1,
        'status': 'proposed_lazy_state_survey_descriptor',
        'manifest_key': key,
        'file': file,
        'protocol_url': f'pmtiles://{file}',
        'state': 'CO',
        'lazy': True,
        'activation_zoom': min(row['activation_zoom'] for row in layers),
        'bounds': list(metadata['bounds']),
        'minzoom': metadata['minzoom'], 'maxzoom': metadata['maxzoom'],
        'layers': layers,
    }


def _stats_manifest(stats, clip_manifest):
    source = {key: value for key, value in stats.items()
              if key not in {'spatial_clip'}}
    spatial = dict(clip_manifest)
    spatial.update(stats['spatial_clip'])
    return source, spatial


def _build_entries(paths, metadata, snapshots, stats, clip_manifest):
    inventories = {}
    layer_sources = {
        'co_cngm_tweto_geology': 'cngm_geology',
        'co_cngm_tweto_faults': 'cngm_faults',
        'co_cgs_on006_quaternary_faults': 'cgs_quaternary',
        'co_cgs_on006_cenozoic_faults': 'cgs_cenozoic',
        'co_cgs_on007_districts': 'districts',
    }
    layer_artifacts = {
        'co_cngm_tweto_geology': 'co_usgs_cngm_tweto_500k',
        'co_cngm_tweto_faults': 'co_usgs_cngm_tweto_500k',
        'co_cgs_on006_quaternary_faults': 'co_cgs_on006_faults',
        'co_cgs_on006_cenozoic_faults': 'co_cgs_on006_faults',
        'co_cgs_on007_districts': 'co_cgs_on007_districts',
    }
    for layer, source_key in layer_sources.items():
        artifact_key = layer_artifacts[layer]
        inventories[layer] = _assert_unique_ids(
            source_key, layer, snapshots.get(source_key), stats[source_key],
            metadata[artifact_key])

    cngm_by_layer = {}
    for source_key in ('cngm_geology', 'cngm_faults'):
        source_inventory, spatial = _stats_manifest(
            stats[source_key], clip_manifest)
        cngm_by_layer[SOURCE_SPECS[source_key]['layer']] = {
            'snapshot': _snapshot_manifest(snapshots[source_key]),
            'source_inventory': source_inventory,
            'spatial_clip': spatial,
            'source_id_inventory': inventories[
                SOURCE_SPECS[source_key]['layer']],
        }
    cngm_n = sum(row['source_inventory']['n']
                 for row in cngm_by_layer.values())

    on006_by_layer = {}
    for source_key in ('cgs_quaternary', 'cgs_cenozoic'):
        source_inventory, spatial = _stats_manifest(
            stats[source_key], clip_manifest)
        on006_by_layer[SOURCE_SPECS[source_key]['layer']] = {
            'snapshot': _snapshot_manifest(snapshots[source_key]),
            'semantic_scope': ('Quaternary' if source_key == 'cgs_quaternary'
                               else 'Cenozoic'),
            'source_inventory': source_inventory,
            'spatial_clip': spatial,
            'source_id_inventory': inventories[
                SOURCE_SPECS[source_key]['layer']],
        }
    on006_n = sum(row['source_inventory']['n']
                  for row in on006_by_layer.values())

    district_inventory, district_spatial = _stats_manifest(
        stats['districts'], clip_manifest)
    entries = {
        'co_usgs_cngm_tweto_500k': {
            'schema_version': 1, 'status': 'baseline_not_release',
            'state': 'CO', 'format': 'pmtiles',
            'file': 'data/tiles/states/co/usgs-cngm-tweto-500k.pmtiles',
            'source_layers': [
                'co_cngm_tweto_geology', 'co_cngm_tweto_faults'],
            'source': {
                'title': 'Geologic map of Colorado (Tweto, 1979)',
                'authority': ('U.S. Geological Survey Cooperative National '
                              'Geologic Map; Colorado Geological Survey lineage'),
                'cngm_item_id': CNGM_ITEM_ID,
                'service': CNGM_SERVICE,
                'map_source_id': CNGM_MAP_SOURCE_ID,
                'map_source_record': CNGM_MAP_SOURCE,
                'map_source_record_sha256': CNGM_MAP_SOURCE_SHA256,
                'data_source_id': CNGM_DATA_SOURCE_ID,
                'data_source_record': CNGM_DATA_SOURCE,
                'data_source_record_sha256': CNGM_DATA_SOURCE_SHA256,
                'publication_id': 'Tweto 1979 / CGS MI-16 / CNGM map50',
                'source_scale': '1:500,000',
            },
            'n': cngm_n, 'states': {'CO': cngm_n},
            'by_layer': cngm_by_layer, 'retrieved': TODAY,
            'required_properties': {
                layer: LAYER_REQUIREMENTS[layer]
                for layer in ('co_cngm_tweto_geology',
                              'co_cngm_tweto_faults')},
            'selection_note': (
                'The exact CNGM map50 subset preserves the Tweto source-map '
                'identity while avoiding a 48-state aggregate download. The '
                'CGS MI-16 catalog exposes a PDF, not a vector dataset.'),
            **_artifact_fields(
                paths['co_usgs_cngm_tweto_500k'],
                metadata['co_usgs_cngm_tweto_500k']),
        },
        'co_cgs_on006_faults': {
            'schema_version': 1, 'status': 'baseline_not_release',
            'state': 'CO', 'format': 'pmtiles',
            'file': 'data/tiles/states/co/cgs-on006-faults.pmtiles',
            'source_layers': [
                'co_cgs_on006_quaternary_faults',
                'co_cgs_on006_cenozoic_faults'],
            'source': {
                'title': 'Colorado Earthquake and Fault Map',
                'authority': 'Colorado Geological Survey',
                'publication_id': 'CGS ON-006-15M',
                'catalog_url': CGS_ON006_CATALOG,
                'webmap_item_id': CGS_WEBMAP_ID,
                'service': CGS_FAULT_SERVICE,
                'source_scale': 'variable (online compilation)',
            },
            'n': on006_n, 'states': {'CO': on006_n},
            'by_layer': on006_by_layer, 'retrieved': TODAY,
            'required_properties': {
                layer: LAYER_REQUIREMENTS[layer]
                for layer in ('co_cgs_on006_quaternary_faults',
                              'co_cgs_on006_cenozoic_faults')},
            'semantic_separation': (
                'Quaternary and Cenozoic service layers remain distinct; '
                'neither label is promoted to a generic activity claim.'),
            **_artifact_fields(
                paths['co_cgs_on006_faults'],
                metadata['co_cgs_on006_faults']),
        },
        'co_cgs_on007_districts': {
            'schema_version': 1, 'status': 'baseline_not_release',
            'state': 'CO', 'format': 'pmtiles',
            'file': 'data/tiles/states/co/cgs-on007-districts.pmtiles',
            'source_layer': 'co_cgs_on007_districts',
            'source': {
                'title': 'Historic Metal Mining Districts of Colorado',
                'authority': 'Colorado Geological Survey',
                'publication_id': 'CGS ON-007-08D v20201112',
                'catalog_url': DISTRICT_CATALOG, 'bulk_url': DISTRICT_URL,
                'bulk_bytes': DISTRICT_BYTES, 'bulk_sha256': DISTRICT_SHA256,
                'native_crs': 'EPSG:26913', 'source_scale': '1:150,000',
                'boundary_character': 'estimated and subjective',
                'archive_member_inventory': DISTRICT_ARCHIVE_INVENTORY,
                'archive_member_inventory_sha256':
                    DISTRICT_ARCHIVE_INVENTORY_SHA256,
            },
            'n': stats['districts']['n'],
            'states': {'CO': stats['districts']['n']},
            'retrieved': TODAY,
            'source_inventory': district_inventory,
            'spatial_clip': district_spatial,
            'source_id_inventory': inventories['co_cgs_on007_districts'],
            'required_properties': LAYER_REQUIREMENTS[
                'co_cgs_on007_districts'],
            'provenance_note': (
                'Every polygon retains its source row, county fields, report '
                'link, 1:150,000 scale, and explicit estimated-boundary status.'),
            **_artifact_fields(
                paths['co_cgs_on007_districts'],
                metadata['co_cgs_on007_districts']),
        },
    }
    for key, entry in entries.items():
        entry['browser_descriptor'] = _browser_descriptor(
            key, entry['file'], metadata[key], stats)
    return entries


def _strict_manifest_bytes():
    with open(MANIFEST, 'rb') as source:
        raw = source.read()
    try:
        manifest = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f'public manifest is invalid JSON: {exc}') from exc
    if not isinstance(manifest, dict):
        raise RuntimeError('public manifest root must be an object')
    return raw, manifest


def _manifest_without_colorado(manifest):
    """Canonical semantic projection excluding only the three CO keys."""
    projected = json.loads(json.dumps(manifest))
    baselines = projected.get('national_baselines')
    if isinstance(baselines, dict):
        for key in BASELINE_KEYS:
            baselines.pop(key, None)
    return projected


def _unrelated_manifest_sha256(manifest):
    return _canonical_sha256(_manifest_without_colorado(manifest))


def _publish(pending, entries):
    """Atomically install the three archives and their manifest entries."""
    if any(value is None for value in SOURCE_SEQUENCE_SHA256.values()):
        raise RuntimeError(
            'Colorado source sequence hashes are not pinned; publication refused')
    if set(pending) != set(BASELINE_KEYS) or set(entries) != set(BASELINE_KEYS):
        raise RuntimeError('Colorado publication requires the exact atomic set')
    for key, path in pending.items():
        if not os.path.isfile(path) or os.path.islink(path):
            raise RuntimeError(f'Colorado pending archive is unsafe: {key}')
    manifest_raw, manifest = _strict_manifest_bytes()
    manifest_sha = hashlib.sha256(manifest_raw).hexdigest()
    unrelated_sha = _unrelated_manifest_sha256(manifest)
    baselines = manifest.setdefault('national_baselines', {})
    if not isinstance(baselines, dict):
        raise RuntimeError('manifest national_baselines must be an object')
    baselines.update(entries)
    if _unrelated_manifest_sha256(manifest) != unrelated_sha:
        raise RuntimeError('Colorado merge changed unrelated manifest content')
    os.makedirs(OUT_DIR, exist_ok=True)
    handle, pending_manifest = tempfile.mkstemp(
        prefix='.manifest-co-state-survey-',
        dir=os.path.dirname(MANIFEST))
    backup_handle, manifest_backup = tempfile.mkstemp(
        prefix='.manifest-co-state-survey-original-',
        dir=os.path.dirname(MANIFEST))
    backups, installed = {}, []
    pending_manifest_sha = None
    success = False
    publication_audit = None
    try:
        os.fchmod(handle, stat.S_IMODE(os.stat(MANIFEST).st_mode))
        with os.fdopen(handle, 'w', encoding='utf-8') as output:
            json.dump(manifest, output, separators=(',', ':'), allow_nan=False)
            output.flush()
            os.fsync(output.fileno())
        pending_manifest_sha = _sha256(pending_manifest)
        os.fchmod(backup_handle, stat.S_IMODE(os.stat(MANIFEST).st_mode))
        with os.fdopen(backup_handle, 'wb') as output:
            output.write(manifest_raw)
            output.flush()
            os.fsync(output.fileno())
        with open(MANIFEST, 'rb') as current:
            if hashlib.sha256(current.read()).hexdigest() != manifest_sha:
                raise RuntimeError('public manifest changed during Colorado build')
        for key, final_path in BASELINE_KEYS.items():
            source_path = pending[key]
            backup = os.path.join(
                os.path.dirname(source_path), f'previous-{key}.pmtiles')
            if os.path.lexists(backup):
                raise RuntimeError(f'Colorado rollback target already exists: {key}')
            if os.path.exists(final_path):
                os.replace(final_path, backup)
                backups[final_path] = backup
            os.replace(source_path, final_path)
            installed.append(final_path)
        with open(MANIFEST, 'rb') as current:
            if hashlib.sha256(current.read()).hexdigest() != manifest_sha:
                raise RuntimeError(
                    'public manifest changed during Colorado publication')
        os.replace(pending_manifest, MANIFEST)
        _, installed_manifest = _strict_manifest_bytes()
        installed_baselines = installed_manifest.get('national_baselines') or {}
        if (_unrelated_manifest_sha256(installed_manifest) != unrelated_sha or
                any(installed_baselines.get(key) != entries[key]
                    for key in BASELINE_KEYS)):
            raise RuntimeError(
                'Colorado post-publication manifest reconciliation failed')
        publication_audit = {
            'status': 'exact_three_key_latest_manifest_merge',
            'keys': sorted(BASELINE_KEYS),
            'manifest_before_sha256': manifest_sha,
            'manifest_after_sha256': _sha256(MANIFEST),
            'unrelated_manifest_sha256': unrelated_sha,
        }
        success = True
    except BaseException as primary:
        rollback_errors = []
        for final_path in reversed(list(BASELINE_KEYS.values())):
            try:
                if final_path in installed and os.path.exists(final_path):
                    os.unlink(final_path)
            except OSError as exc:
                rollback_errors.append(f'{final_path}: remove new: {exc}')
            backup = backups.get(final_path)
            try:
                if backup and os.path.exists(backup):
                    os.replace(backup, final_path)
            except OSError as exc:
                rollback_errors.append(f'{final_path}: restore old: {exc}')
        try:
            current_sha = _sha256(MANIFEST)
            if pending_manifest_sha is not None and \
                    current_sha == pending_manifest_sha:
                os.replace(manifest_backup, MANIFEST)
            elif current_sha != manifest_sha:
                # Preserve a concurrent writer's document.  The Colorado
                # archives have already been rolled back above.
                pass
        except OSError as exc:
            rollback_errors.append(f'manifest rollback: {exc}')
        if rollback_errors:
            raise RuntimeError(
                'Colorado publication rollback failed; preserved backups: ' +
                '; '.join(rollback_errors)) from primary
        raise
    finally:
        try:
            os.unlink(pending_manifest)
        except FileNotFoundError:
            pass
        try:
            os.close(backup_handle)
        except OSError:
            pass
        try:
            os.unlink(manifest_backup)
        except FileNotFoundError:
            pass
        for backup in backups.values() if success else ():
            try:
                os.unlink(backup)
            except FileNotFoundError:
                pass
    return publication_audit


def _artifact_layers():
    return {
        'co_usgs_cngm_tweto_500k': (
            'co_cngm_tweto_geology', 'co_cngm_tweto_faults'),
        'co_cgs_on006_faults': (
            'co_cgs_on006_quaternary_faults',
            'co_cgs_on006_cenozoic_faults'),
        'co_cgs_on007_districts': ('co_cgs_on007_districts',),
    }


def _tile_set(directory, sequences):
    os.makedirs(directory, exist_ok=False)
    local_sequences = {}
    for key, source in sequences.items():
        target = os.path.join(directory, f'{key}.geojsonseq')
        shutil.copyfile(source, target)
        if (os.path.getsize(target) != os.path.getsize(source) or
                _sha256(target) != _sha256(source)):
            raise RuntimeError(f'{key} private build input copy changed')
        local_sequences[key] = target
    paths = {
        'co_usgs_cngm_tweto_500k': os.path.join(
            directory, 'usgs-cngm-tweto-500k.pmtiles'),
        'co_cgs_on006_faults': os.path.join(
            directory, 'cgs-on006-faults.pmtiles'),
        'co_cgs_on007_districts': os.path.join(
            directory, 'cgs-on007-districts.pmtiles'),
    }
    _run_tippecanoe(paths['co_usgs_cngm_tweto_500k'], (
        ('co_cngm_tweto_geology', local_sequences['cngm_geology']),
        ('co_cngm_tweto_faults', local_sequences['cngm_faults'])),
        'USGS CNGM map50; Tweto 1979; Colorado Geological Survey MI-16')
    _run_tippecanoe(paths['co_cgs_on006_faults'], (
        ('co_cgs_on006_quaternary_faults', local_sequences['cgs_quaternary']),
        ('co_cgs_on006_cenozoic_faults', local_sequences['cgs_cenozoic'])),
        'Colorado Geological Survey ON-006-15M')
    _run_tippecanoe(paths['co_cgs_on007_districts'], (
        ('co_cgs_on007_districts', local_sequences['districts']),),
        'Colorado Geological Survey ON-007-08D v20201112')
    return paths


def _validate_set(paths):
    return {
        key: _validate_pmtiles(path, _artifact_layers()[key])
        for key, path in paths.items()}


def _preflight():
    if not shutil.which('tippecanoe'):
        raise RuntimeError('tippecanoe is required')
    if _tippecanoe_version() != TIPPECANOE_VERSION:
        raise RuntimeError(
            f'tippecanoe must remain pinned at {TIPPECANOE_VERSION}')
    if fiona is None or Transformer is None:
        raise RuntimeError(
            'Fiona and pyproj are required; use '
            '/Users/matthewlew/miniconda3/bin/python')
    if any(value is None for value in (
            shapely, shapely_make_valid, shapely_mapping, shapely_shape,
            shapely_transform, shapely_unary_union, shapely_prepare,
            shapely_explain_validity)):
        raise RuntimeError('Shapely 2.x is required')
    if (getattr(shapely, '__version__', None) != '2.0.3' or
            getattr(shapely, 'geos_version_string', None) != '3.11.3'):
        raise RuntimeError(
            'geometry repair audit requires Shapely 2.0.3 / GEOS 3.11.3')
    # Import before source work so a missing semantic validator fails early.
    from validate_national import _pmtiles_header  # noqa: F401


def build(*, publish=False, grace_seconds=0, double_build=True):
    """Build privately by default; publish only with explicit authorization."""
    _preflight()
    if not 0 <= grace_seconds <= 60:
        raise RuntimeError('manifest grace must be from 0 to 60 seconds')
    if publish and not double_build:
        raise RuntimeError('Colorado publication requires a deterministic double build')
    staging = _ensure_private_staging_root()
    with tempfile.TemporaryDirectory(
            prefix='nwmm-co-baselines-', dir=staging) as temp:
        authority_items = _verify_authority_items()
        source_bindings = _verify_cngm_source_bindings()
        snapshots = {key: _layer_snapshot(key) for key in SOURCE_SPECS}
        clip = _load_co_clip()
        archive_path = os.path.join(temp, 'ON-007-08D-v20201112.zip')
        district_download = _download_district_zip(archive_path)
        district_shapefile = _extract_district_shapefile(archive_path, temp)

        passes, pass_stats = [], []
        for pass_number in (1, 2):
            sequences = {
                key: os.path.join(temp, f'pass-{pass_number}-{key}.geojsonseq')
                for key in (*SOURCE_SPECS, 'districts')}
            stats = {
                key: _stream_arcgis(key, snapshots[key], sequences[key], clip)
                for key in SOURCE_SPECS}
            stats['districts'] = _stream_districts(
                district_shapefile, sequences['districts'], clip)
            passes.append(sequences)
            pass_stats.append(stats)
        for key in (*SOURCE_SPECS, 'districts'):
            if (_sha256(passes[0][key]) != _sha256(passes[1][key]) or
                    os.path.getsize(passes[0][key]) !=
                    os.path.getsize(passes[1][key]) or
                    pass_stats[0][key] != pass_stats[1][key]):
                raise RuntimeError(f'{key} changed across full source passes')

        # A full postflight catches membership/schema drift during the passes.
        if _verify_cngm_source_bindings() != source_bindings:
            raise RuntimeError('CNGM source bindings changed during build')
        if _verify_authority_items() != authority_items:
            raise RuntimeError('official ArcGIS item identity changed during build')
        postflight = {key: _layer_snapshot(key) for key in SOURCE_SPECS}
        for key in SOURCE_SPECS:
            if _snapshot_manifest(postflight[key]) != \
                    _snapshot_manifest(snapshots[key]):
                raise RuntimeError(f'{key} snapshot changed during build')

        first_paths = _tile_set(os.path.join(temp, 'tile-set-a'), passes[0])
        first_metadata = _validate_set(first_paths)
        path_independent_metadata = {
            key: _assert_path_independent_metadata(first_paths[key], key)
            for key in BASELINE_KEYS}
        entries = _build_entries(
            first_paths, first_metadata, snapshots, pass_stats[0],
            clip['manifest'])
        deterministic = {}
        if double_build:
            second_paths = _tile_set(
                os.path.join(temp, 'tile-set-b'), passes[0])
            second_metadata = _validate_set(second_paths)
            second_path_metadata = {
                key: _assert_path_independent_metadata(second_paths[key], key)
                for key in BASELINE_KEYS}
            if second_path_metadata != path_independent_metadata:
                raise RuntimeError(
                    'Colorado PMTiles metadata is path-dependent across builds')
            _build_entries(
                second_paths, second_metadata, snapshots, pass_stats[0],
                clip['manifest'])
            for key in BASELINE_KEYS:
                first = (os.path.getsize(first_paths[key]),
                         _sha256(first_paths[key]))
                second = (os.path.getsize(second_paths[key]),
                          _sha256(second_paths[key]))
                if first != second:
                    raise RuntimeError(
                        f'{key} PMTiles build is nondeterministic: '
                        f'{first} != {second}')
                deterministic[key] = {
                    'status': 'two_byte_identical_builds',
                    'bytes': first[0], 'sha256': first[1],
                }
                entries[key]['deterministic_rebuild'] = deterministic[key]

        report = {
            'status': ('published_baseline_not_release' if publish
                       else 'private_baseline_not_release'),
            'state': 'CO', 'release_changed': False,
            'source_snapshots': {
                key: _snapshot_manifest(value)
                for key, value in snapshots.items()},
            'cngm_source_bindings': source_bindings,
            'source_sequences': {
                key: {
                    'records': pass_stats[0][key]['source_records'],
                    'tiled': pass_stats[0][key]['n'],
                    'bytes': pass_stats[0][key]['sequence_bytes'],
                    'sha256': pass_stats[0][key]['sequence_sha256'],
                }
                for key in (*SOURCE_SPECS, 'districts')},
            'district_download': district_download,
            'artifacts': {
                key: {
                    'features': entries[key]['n'],
                    'bytes': entries[key]['bytes'],
                    'sha256': entries[key]['sha256'],
                    'bounds': entries[key]['bounds'],
                    'minzoom': entries[key]['minzoom'],
                    'maxzoom': entries[key]['maxzoom'],
                    'source_layers': (entries[key].get('source_layers') or
                                      [entries[key]['source_layer']]),
                    'field_types': entries[key]['field_types'],
                    'semantic_tile_feature_counts': entries[key][
                        'semantic_tile_feature_counts'],
                    'maxzoom_feature_instances': first_metadata[key][
                        'maxzoom_feature_instances'],
                    'maxzoom_unique_feature_ids': {
                        layer: len(ids) for layer, ids in first_metadata[key][
                            'maxzoom_feature_ids'].items()},
                }
                for key in BASELINE_KEYS},
            'browser_descriptors': {
                key: entries[key]['browser_descriptor']
                for key in BASELINE_KEYS},
            'deterministic_rebuild': deterministic,
            'path_independent_pmtiles_metadata': path_independent_metadata,
        }
        publication_audit = None
        if publish:
            print('Colorado private archives validated; atomic publication '
                  f'begins in {grace_seconds} seconds')
            if grace_seconds:
                time.sleep(grace_seconds)
            publication_audit = _publish(first_paths, entries)
            report['publication_audit'] = publication_audit
        print(json.dumps(report, indent=2, sort_keys=True))
        return report


def _entry_layers(entry):
    return (entry.get('source_layers') or [entry.get('source_layer')])


def validate_manifest_baselines(manifest, *, pmtiles_header=None):
    """Validate a future published atomic Colorado set without network access."""
    baselines = manifest.get('national_baselines')
    if not isinstance(baselines, dict):
        raise RuntimeError('manifest national_baselines is missing')
    result = {}
    source_contract_by_layer = {
        'co_cngm_tweto_geology': ARCGIS_SNAPSHOT_CONTRACTS['cngm_geology'],
        'co_cngm_tweto_faults': ARCGIS_SNAPSHOT_CONTRACTS['cngm_faults'],
        'co_cgs_on006_quaternary_faults':
            ARCGIS_SNAPSHOT_CONTRACTS['cgs_quaternary'],
        'co_cgs_on006_cenozoic_faults':
            ARCGIS_SNAPSHOT_CONTRACTS['cgs_cenozoic'],
        'co_cgs_on007_districts': {
            'n': 383,
            'object_ids_sha256': _canonical_sha256(list(range(1, 384))),
        },
    }
    browser_stats = {
        source_key: {'n': contract['n']}
        for source_key, contract in ARCGIS_SNAPSHOT_CONTRACTS.items()
    }
    browser_stats['districts'] = {'n': 383}
    for key, expected_path in BASELINE_KEYS.items():
        entry = baselines.get(key)
        if (not isinstance(entry, dict) or entry.get('schema_version') != 1 or
                entry.get('status') != 'baseline_not_release' or
                entry.get('state') != 'CO' or entry.get('format') != 'pmtiles' or
                entry.get('file') != os.path.relpath(expected_path, SITE)):
            raise RuntimeError(f'{key} manifest baseline schema is invalid')
        if not os.path.isfile(expected_path):
            raise RuntimeError(f'{key} PMTiles artifact is missing')
        layers = _entry_layers(entry)
        if layers != list(_artifact_layers()[key]):
            raise RuntimeError(f'{key} source-layer contract is invalid')
        metadata = _validate_pmtiles(
            expected_path, layers, pmtiles_header=pmtiles_header)
        if (entry.get('bytes') != os.path.getsize(expected_path) or
                entry.get('sha256') != _sha256(expected_path) or
                entry.get('bounds') != metadata['bounds'] or
                entry.get('minzoom') != metadata['minzoom'] or
                entry.get('maxzoom') != metadata['maxzoom'] or
                entry.get('field_types') != metadata['field_types'] or
                entry.get('reproducible_metadata') !=
                metadata['reproducible_metadata'] or
                entry.get('semantic_tile_feature_counts') !=
                metadata['semantic_layer_counts']):
            raise RuntimeError(f'{key} artifact fields do not reconcile')
        expected_browser = _browser_descriptor(
            key, entry['file'], metadata, browser_stats)
        if entry.get('browser_descriptor') != expected_browser:
            raise RuntimeError(f'{key} browser descriptor is invalid')
        by_layer = entry.get('by_layer') or {}
        for layer in layers:
            if key == 'co_cgs_on007_districts':
                inventory = entry.get('source_id_inventory')
            else:
                inventory = (by_layer.get(layer) or {}).get(
                    'source_id_inventory')
            ids = metadata['maxzoom_feature_ids'].get(layer) or []
            contract = source_contract_by_layer[layer]
            if (not isinstance(inventory, dict) or
                    inventory.get('status') != 'complete' or
                    inventory.get('source_records') != contract['n'] or
                    inventory.get('unique_maxzoom_ids') != len(ids) or
                    inventory.get('maxzoom_object_ids_sha256') !=
                    _canonical_sha256(ids) or len(ids) != contract['n'] or
                    _canonical_sha256(ids) != contract['object_ids_sha256']):
                raise RuntimeError(f'{key}/{layer} exact ID inventory is invalid')
        n = entry.get('n')
        if (not isinstance(n, int) or isinstance(n, bool) or n <= 0 or
                entry.get('states') != {'CO': n}):
            raise RuntimeError(f'{key} feature count is invalid')
        result[key] = {
            'features': n, 'bytes': entry['bytes'],
            'sha256': entry['sha256']}
    district = baselines['co_cgs_on007_districts'].get('source') or {}
    if (district.get('bulk_bytes') != DISTRICT_BYTES or
            district.get('bulk_sha256') != DISTRICT_SHA256 or
            district.get('archive_member_inventory') !=
            DISTRICT_ARCHIVE_INVENTORY or
            district.get('archive_member_inventory_sha256') !=
            DISTRICT_ARCHIVE_INVENTORY_SHA256):
        raise RuntimeError('Colorado district source ZIP contract is invalid')
    cngm = baselines['co_usgs_cngm_tweto_500k'].get('source') or {}
    if (cngm.get('map_source_id') != CNGM_MAP_SOURCE_ID or
            cngm.get('map_source_record') != CNGM_MAP_SOURCE or
            cngm.get('map_source_record_sha256') != CNGM_MAP_SOURCE_SHA256 or
            cngm.get('data_source_id') != CNGM_DATA_SOURCE_ID or
            cngm.get('data_source_record') != CNGM_DATA_SOURCE or
            cngm.get('data_source_record_sha256') !=
            CNGM_DATA_SOURCE_SHA256):
        raise RuntimeError('Colorado CNGM source-map contract is invalid')
    return result


def check():
    _, manifest = _strict_manifest_bytes()
    result = validate_manifest_baselines(manifest)
    print(json.dumps(result, indent=2, sort_keys=True))
    return result


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--check', action='store_true',
                        help='validate a future published atomic baseline set')
    parser.add_argument('--publish', action='store_true',
                        help='atomically publish; default is private and ephemeral')
    parser.add_argument('--single-build', action='store_true',
                        help='private diagnostic only; publication forbids this')
    parser.add_argument('--manifest-grace-seconds', type=int, default=0,
                        help='bounded coordination window before publication')
    args = parser.parse_args(argv)
    if args.check:
        if args.publish or args.single_build:
            parser.error('--check cannot be combined with build options')
        check()
    else:
        build(publish=args.publish,
              grace_seconds=args.manifest_grace_seconds,
              double_build=not args.single_build)


if __name__ == '__main__':
    main()
