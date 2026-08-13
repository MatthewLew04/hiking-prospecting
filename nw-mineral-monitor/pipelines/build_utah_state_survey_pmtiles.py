#!/usr/bin/env python3
"""Build exact Utah state-survey PMTiles without releasing Utah.

The four-archive baseline is deliberately publication-bound:

* UGS Map 179DM, the official 1:500,000 statewide geology GIS package;
* UGS Data Series 7, the March 2026 replacement for older Utah Quaternary
  fault copies;
* UGS Open-File Report 695 mining-district polygons; and
* UGS Open-File Report 757, the versioned Utah Mineral Occurrence System.

Every official ZIP is checksum and member-inventory pinned.  Source layers
are read twice from private ``build-inputs/.staging`` paths, normalized
sequences must be byte-identical, and two unrelated tile directories must
produce byte-identical PMTiles.  Validation scans required properties at all
zooms and exact feature IDs at maximum zoom.  Raw statewide vectors never
enter ``site/``.

The default action is a private audit.  Publication requires both
``--publish`` and a 30-second grace interval, installs the four archives and
their manifest entries as one rollback-safe transaction, and never changes a
Utah release flag or DONE gate.
"""
from __future__ import annotations

import argparse
from collections import Counter
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
import urllib.request
import zipfile
import zlib

from common import TODAY

try:
    import fiona
except ImportError:  # pragma: no cover - build preflight explains runtime
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
except ImportError:  # pragma: no cover - build preflight explains runtime
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
except ImportError:  # pragma: no cover - build preflight explains runtime
    Transformer = None


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
SITE = os.path.join(ROOT, 'site')
MANIFEST = os.path.join(SITE, 'data', 'manifest.json')
PRIVATE_STAGING_ROOT = os.path.join(ROOT, 'build-inputs', '.staging')
STATE_CLIPS = os.path.join(ROOT, 'infra', 'state_clips.json')
OUT_DIR = os.path.join(SITE, 'data', 'tiles', 'states', 'ut')

MAP179_OUT = os.path.join(OUT_DIR, 'ugs-map179dm-500k.pmtiles')
DS7_OUT = os.path.join(OUT_DIR, 'ugs-ds7-quaternary-faults.pmtiles')
DISTRICTS_OUT = os.path.join(OUT_DIR, 'ugs-ofr695-mining-districts.pmtiles')
UMOS_OUT = os.path.join(OUT_DIR, 'ugs-ofr757-umos.pmtiles')
BASELINE_KEYS = {
    'ut_ugs_map179dm_500k': MAP179_OUT,
    'ut_ugs_ds7_quaternary_faults': DS7_OUT,
    'ut_ugs_ofr695_mining_districts': DISTRICTS_OUT,
    'ut_ugs_ofr757_umos': UMOS_OUT,
}
ATOMIC_GROUP_ID = 'ut_ugs_state_survey_baselines_v1'

MAP179_URL = (
    'https://ugspub.nr.utah.gov/publications/GIS_maps/'
    'GeologicMapOfUtah.zip')
DS7_URL = (
    'https://ugspub.nr.utah.gov/publications/data_series/ds-7/ds-7.zip')
DISTRICTS_URL = (
    'https://geology.utah.gov/apps/blm_mineral/appfiles/'
    'Mining_Districts_20190116gdb.zip')
UMOS_URL = (
    'https://ugspub.nr.utah.gov/publications/open_file_reports/'
    'ofr-757/ofr-757.zip')

GEOLOGY_CATALOG = 'https://geology.utah.gov/map-pub/maps/geologic-maps/'
MINERAL_CATALOG = 'https://geology.utah.gov/apps/jay/minerals/'
HAZARD_CATALOG = 'https://geology.utah.gov/apps/publications/hazFilter.php'

ARCHIVE_CONTRACTS = {
    'map179': {
        'url': MAP179_URL, 'filename': 'GeologicMapOfUtah.zip',
        'bytes': 27_317_100,
        'sha256':
            'df02e3692fbf5c2cc64fa143c364cd9e3f10472f97bb94277af07db0fe281484',
        'member_count': 17,
        'member_inventory_sha256':
            '07eb6b0e039296a24b2b6fbfd899701ff91e9be652b51b0118eea2aeeb0163b8',
        'layers': ('Geology_arc', 'Geology_poly'),
        'extract': False,
    },
    'ds7': {
        'url': DS7_URL, 'filename': 'ds-7.zip', 'bytes': 4_478_185,
        'sha256':
            '7b64620d0f6411891daa172e34fe994bbd5d1531a83e6a48393eea601fec905d',
        'member_count': 79,
        'member_inventory_sha256':
            'e30a8417c85d80cef93f6851b9ababb8bfb1f93c7176e77b5800e862e3c04459',
        'layers': (
            'UQFD25_DS7_full', 'UQD25_DS7_SSZ_full',
            'UQFD25_DS7_SSZnew', 'UQFD25_DS7_new'),
        'extract': True, 'gdb': 'UQFD25_DS7_GIS.gdb',
    },
    'districts': {
        'url': DISTRICTS_URL,
        'filename': 'Mining_Districts_20190116gdb.zip',
        'bytes': 36_391_387,
        'sha256':
            '7e298b2f9dfc130120c1cc7f2db1f894b1c1341d2c67e47cfd4165c7bfe244e6',
        'member_count': 58,
        'member_inventory_sha256':
            '4717264ac1472dbda82343a19ea39a3d37870a2b691f69a3c7db89081e2cde65',
        'layers': ('mining_districts', 'mining_districts__ATTACH',
                   'Match_Table'),
        'extract': True, 'gdb': 'Mining_Districts_20190116.gdb',
    },
    'umos': {
        'url': UMOS_URL, 'filename': 'ofr-757.zip', 'bytes': 5_880_910,
        'sha256':
            '2da50d3ebd41c914d5472111030e57e4f6b9812e78c195e251a2a44fbc9f64c7',
        'member_count': 51,
        'member_inventory_sha256':
            '226dc58be71895038af5dd6f89011402060617a1026b3509dcb91c3f3e2bf423',
        'layers': ('UMOS_2023_08_25',),
        'extract': True, 'gdb': 'UMOS.gdb',
    },
}

SOURCE_SPECS = {
    'geology_lines': {
        'archive': 'map179', 'layer': 'Geology_arc', 'kind': 'line',
        'fid_shift': 1, 'layer_id': 'ut_ugs_map179dm_structures',
        'native_crs': 'EPSG:26712',
        'manifest_sha256':
            '1547e6cdcf9e815751fffcf48bddaf544afe8e0130720fbe257ac44406a5dc3c',
        'source_fids_sha256':
            'cfbe1ce5c5668e09e32506d1bcc1893b5f7b77c09027ff8ecf0f25b52ac1a584',
    },
    'geology_units': {
        'archive': 'map179', 'layer': 'Geology_poly', 'kind': 'polygon',
        'fid_shift': 1, 'layer_id': 'ut_ugs_map179dm_geology',
        'native_crs': 'EPSG:26712',
        'manifest_sha256':
            '8a4189b45d263ceb37e28d6ae2b1ff9fbc5cc1d6094edadcd4ea95f18a493424',
        'source_fids_sha256':
            '7efbcaeb02aa7e8580b354e1d0e9be64166af1a1d0e26e6f50c611af7b6e3e5f',
    },
    'faults': {
        'archive': 'ds7', 'layer': 'UQFD25_DS7_full', 'kind': 'line',
        'fid_shift': 0, 'layer_id': 'ut_ugs_ds7_quaternary_faults',
        'native_crs': 'EPSG:26912',
        'manifest_sha256':
            '98757ff140557cbf70bf317c10097634eee6502201e1dc13d70a3778c9472429',
        'source_fids_sha256':
            '42092bb005917df71cfe0b7f728584a38285525981669c2318fe6103ddb96377',
    },
    'districts': {
        'archive': 'districts', 'layer': 'mining_districts',
        'kind': 'polygon', 'fid_shift': 0,
        'layer_id': 'ut_ugs_ofr695_mining_districts',
        'native_crs': 'EPSG:26712',
        'manifest_sha256':
            'dcb17f474c58ae4550473214d31903e84ac0b0221d82a7f476d84235dc7471f4',
        'source_fids_sha256':
            'f49f468eae355470508cf712d18d1fc25e98b967394c26cbf4fe99f638a3c768',
    },
    'umos': {
        'archive': 'umos', 'layer': 'UMOS_2023_08_25', 'kind': 'point',
        'fid_shift': 0, 'layer_id': 'ut_ugs_ofr757_umos',
        'native_crs': 'EPSG:26912',
        'manifest_sha256':
            '3f6e3c89dabce86cdac33e5be0b247616591d0016a2d772912a2a7d127d5945c',
        'source_fids_sha256':
            '37bfffa43d808e5db87886d454d5fbef7b2fde644ecf8f17b184437de3ea430a',
    },
}

EMPTY_SHA256 = (
    '4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945')

# These contracts are filled from a complete source audit and guard every
# exclusion/clip class.  Output geometry types are pinned after normalization.
GEOMETRY_CONTRACTS = {
    'geology_lines': {
        'source_records': 68_126, 'source_types': {'LineString': 68_126},
        'source_object_ids_sha256':
            '11a87029ac83a486ce1234a1ca9de3078f9c069cfa853ca2a1fc7f958baf11d1',
        'output_types': {'LineString': 67_563, 'MultiLineString': 8},
        'empty_count': 0, 'empty_sha256': EMPTY_SHA256,
        'unusable_count': 2,
        'unusable_sha256':
            '446aeee61da8b7941cb75277b789638e5b35ec432efe2497daafff33edadb861',
        'outside_count': 552,
        'outside_sha256':
            'faf6320156887a391ed04121b56b9e42f1544dd352a5a5a3497eef14def5118c',
        'clipped_count': 520,
        'clipped_sha256':
            'f183b0348a0d365c38d6a5b5b0de55a99628ac0e77a647e83133b122045c713e',
        'repair_count': 0, 'repair_sha256': EMPTY_SHA256,
        'z_count': 0, 'z_sha256': EMPTY_SHA256,
        'z_coordinate_count': 0, 'z_zero_coordinate_count': 0,
        'z_nonzero_coordinate_count': 0,
        'z_nonzero_fids_sha256': EMPTY_SHA256,
        'z_nonzero_records_sha256': EMPTY_SHA256,
    },
    'geology_units': {
        'source_records': 22_637, 'source_types': {'Polygon': 22_637},
        'source_object_ids_sha256':
            '2cc1b41f8a605edc1c422e8ced9d5bcc894499dc8f7061029646b4ca9450f375',
        'output_types': {'MultiPolygon': 3, 'Polygon': 22_632},
        'empty_count': 0, 'empty_sha256': EMPTY_SHA256,
        'unusable_count': 0, 'unusable_sha256': EMPTY_SHA256,
        'outside_count': 2,
        'outside_sha256':
            '6094581059c8f5f1f3b979755dfc11bc091c9f5f947b52e5bb13e3241af83a94',
        'clipped_count': 462,
        'clipped_sha256':
            'aaee78894ac66bf164aa036e01276011b52bbde1c43f9dec8b7be5548e0e19c1',
        'repair_count': 0, 'repair_sha256': EMPTY_SHA256,
        'z_count': 0, 'z_sha256': EMPTY_SHA256,
        'z_coordinate_count': 0, 'z_zero_coordinate_count': 0,
        'z_nonzero_coordinate_count': 0,
        'z_nonzero_fids_sha256': EMPTY_SHA256,
        'z_nonzero_records_sha256': EMPTY_SHA256,
    },
    'faults': {
        'source_records': 19_743,
        'source_types': {'MultiLineString': 19_743},
        'source_object_ids_sha256':
            '42092bb005917df71cfe0b7f728584a38285525981669c2318fe6103ddb96377',
        'output_types': {'LineString': 19_232},
        'empty_count': 0, 'empty_sha256': EMPTY_SHA256,
        'unusable_count': 0, 'unusable_sha256': EMPTY_SHA256,
        'outside_count': 502,
        'outside_sha256':
            '043a82d4fb9210580505caff61b2cd9d4d62baf671d16f447944a1088bb7efa1',
        'clipped_count': 35,
        'clipped_sha256':
            '4619f0eb36d0a3facd81e44e47203345ab7cfc951f75db5932b9af1d33c58ab9',
        'repair_count': 0, 'repair_sha256': EMPTY_SHA256,
        'z_count': 19_743,
        'z_sha256':
            '42092bb005917df71cfe0b7f728584a38285525981669c2318fe6103ddb96377',
        'z_coordinate_count': 245_248,
        'z_zero_coordinate_count': 245_245,
        'z_nonzero_coordinate_count': 3,
        'z_nonzero_fids_sha256':
            '80e99744819fe75ccb3cfa7c1a032a4b37bb5dee931017c9adcad388e07f74f4',
        'z_nonzero_records_sha256':
            '7529120ad539eda2c5eda66ff84184e3b8576ea9481f94f1577f89bec86be94c',
    },
    'districts': {
        'source_records': 185, 'source_types': {'MultiPolygon': 185},
        'source_object_ids_sha256':
            'f49f468eae355470508cf712d18d1fc25e98b967394c26cbf4fe99f638a3c768',
        'output_types': {'Polygon': 185},
        'empty_count': 0, 'empty_sha256': EMPTY_SHA256,
        'unusable_count': 0, 'unusable_sha256': EMPTY_SHA256,
        'outside_count': 0, 'outside_sha256': EMPTY_SHA256,
        'clipped_count': 12,
        'clipped_sha256':
            'c670b78fe58d943cacf6e3cb9459a9728e003afadfe2fb405ac33e6e89e17b2e',
        'repair_count': 0, 'repair_sha256': EMPTY_SHA256,
        'z_count': 0, 'z_sha256': EMPTY_SHA256,
        'z_coordinate_count': 0, 'z_zero_coordinate_count': 0,
        'z_nonzero_coordinate_count': 0,
        'z_nonzero_fids_sha256': EMPTY_SHA256,
        'z_nonzero_records_sha256': EMPTY_SHA256,
    },
    'umos': {
        'source_records': 7_793, 'source_types': {'Point': 7_792},
        'source_object_ids_sha256':
            '37bfffa43d808e5db87886d454d5fbef7b2fde644ecf8f17b184437de3ea430a',
        'output_types': {'Point': 7_787},
        'empty_count': 1,
        'empty_sha256':
            '8a67a07291e49ae899e2a2fb40b958bd8a5d6555e842e5fec0da80dc83c54e76',
        'unusable_count': 0, 'unusable_sha256': EMPTY_SHA256,
        'outside_count': 5,
        'outside_sha256':
            '3dd129fcb86c939c9b844fb9e4f6d0154225dc842e63474c872a7f5aa674ec45',
        'clipped_count': 0, 'clipped_sha256': EMPTY_SHA256,
        'repair_count': 0, 'repair_sha256': EMPTY_SHA256,
        'z_count': 0, 'z_sha256': EMPTY_SHA256,
        'z_coordinate_count': 0, 'z_zero_coordinate_count': 0,
        'z_nonzero_coordinate_count': 0,
        'z_nonzero_fids_sha256': EMPTY_SHA256,
        'z_nonzero_records_sha256': EMPTY_SHA256,
    },
}

# Filled only after a full private two-pass source audit.  Publication refuses
# null values even though private audits may print new fingerprints for review.
SOURCE_SEQUENCE_SHA256 = {
    'geology_lines':
        '92100bcb0972db8e077f1ee8ede144c91ec685f826c424aeb9f9bc21d05600e3',
    'geology_units':
        'ba81993897a0670aa6c73e4eb439dad36cc98064f54f2d00c818dd6d9c558a95',
    'faults':
        'a94c6460bdda343ab4709dbd4d8be50a81be7abde7f39288c61337f08f3ab413',
    'districts':
        '63e824d76c593ebd124616c3fb735f3508b096220f84d7715748afbc12fae59b',
    'umos':
        'de474e7db4e2829b976ee2f40f4c59a7eacda5b870bccbc08c7314e3336d7cba',
}
SOURCE_SEQUENCE_BYTES = {
    'geology_lines': 64_028_641,
    'geology_units': 51_114_310,
    'faults': 32_488_559,
    'districts': 659_729,
    'umos': 15_790_119,
}

# Any valid source geometry that cannot survive z12/full-detail-14 MVT
# quantization is recorded here after private review and omitted explicitly.
ENCODING_EXCLUSIONS = {
    'geology_lines': [{
        'fid': 22_207, 'source_record_id': '22206',
        'source_feature_type': 'water boundary',
        'source_coordinate_count': 2,
        'source_native_length_m': 0.0625,
        'source_geometry_sha256':
            'a1cd0765ae5411da3444cc823642f35c76f3eb1f57a47bc970d08426b453b2b9',
        'output_coordinate_count': 2,
        'output_web_mercator_length_m': 0.0817746493674161,
        'output_geometry_sha256':
            '11e188002383fbe92c61319ba763aa9e09b22ae9ae71b21cdbfa7e37956c14bd',
        'tippecanoe_maxzoom': 12, 'tippecanoe_full_detail': 14,
        'z12_full_detail14_web_mercator_unit_m': 0.5971642834779395,
        'review': ('valid two-vertex source line is shorter than one z12/full14 '
                   'Web Mercator encoding unit and was absent from the complete '
                   'maxzoom top-level-ID scan'),
    }], 'geology_units': [], 'faults': [{
        'fid': 813, 'source_record_id': '813', 'mapped_scale': '1:10,000',
        'source_coordinate_count': 2,
        'source_native_length_m': 0.3396606748921972,
        'source_geometry_sha256':
            '2f8a0701994ef3084268e1c58a10502863123c22d442f309e97aa6880a311cc7',
        'state_clipped': False,
        'output_web_mercator_length_m': 0.4520954398908576,
        'output_geometry_sha256':
            '3cffb56aa9061bfa2c3d54bb4693c816f9d9126fcd82ea14dd38cc0d63e97373',
    }, {
        'fid': 885, 'source_record_id': '885', 'mapped_scale': '1:10,000',
        'source_coordinate_count': 2,
        'source_native_length_m': 0.03927986299369752,
        'source_geometry_sha256':
            '1f2e1944dd6d3ac44f7efd8e9b280f709979c35f25901bbfd25f370d4d8a1ec5',
        'state_clipped': False,
        'output_web_mercator_length_m': 0.05233976781393746,
        'output_geometry_sha256':
            '6ed443fea31cb05123ba450355f6e978fa63d6cb4a5b213ea3f318dd516764ed',
    }, {
        'fid': 901, 'source_record_id': '901', 'mapped_scale': '1:10,000',
        'source_coordinate_count': 2,
        'source_native_length_m': 0.07630595071034345,
        'source_geometry_sha256':
            'fc62a29b0217183ee06558c469acde243faf1029b52cd955097dd8f1aadfdd97',
        'state_clipped': False,
        'output_web_mercator_length_m': 0.10159689037396819,
        'output_geometry_sha256':
            '4b03efe3db9426a8930f3d1b36f7701b1e8ae3a60ecd9c033c366c4f3c54da31',
    }, {
        'fid': 1_615, 'source_record_id': '1615',
        'mapped_scale': '1:10,000', 'source_coordinate_count': 2,
        'source_native_length_m': 0.011030880684478963,
        'source_geometry_sha256':
            '7428d85f6c806a1850f249d18d54028bd05bd6e12da904b785edd1465cc583cd',
        'state_clipped': False,
        'output_web_mercator_length_m': 0.014673295653765366,
        'output_geometry_sha256':
            'd45dcf67df50879f953b6dcb9f65e63b67cff841cfad1bfe1246d1dd24929df8',
    }, {
        'fid': 2_834, 'source_record_id': '2834',
        'mapped_scale': '1:10,000', 'source_coordinate_count': 2,
        'source_native_length_m': 0.273953181370275,
        'source_geometry_sha256':
            'c4e463efa6eb428aebbbaa0c09a6deddd99c4e3a150201f224f3379af210e3ae',
        'state_clipped': False,
        'output_web_mercator_length_m': 0.35965398192355286,
        'output_geometry_sha256':
            '1a1cf3cff4190752a319437fa313a7ea5b6c3f142200def656483bd17f4921e0',
    }, {
        'fid': 2_932, 'source_record_id': '2932',
        'mapped_scale': '1:10,000', 'source_coordinate_count': 2,
        'source_native_length_m': 0.1800447482121338,
        'source_geometry_sha256':
            'aa6e5cfe8bcf57203bbafb7d1fd7dfce513073ad779f4b7100ed286a25167721',
        'state_clipped': False,
        'output_web_mercator_length_m': 0.23716476705366846,
        'output_geometry_sha256':
            '4479fa936ae0b4abd6db1365d37630df35f400bef28f0d95cdf6c927fdaeb479',
    }, {
        'fid': 2_967, 'source_record_id': '2967',
        'mapped_scale': '1:10,000', 'source_coordinate_count': 2,
        'source_native_length_m': 0.15147161257098912,
        'source_geometry_sha256':
            '5b3fd5a9cd6f28c5e3551500906546a4db6b17769a63235c89d2299049eba4a9',
        'state_clipped': False,
        'output_web_mercator_length_m': 0.1989431734350277,
        'output_geometry_sha256':
            '111c16679579d29eaf315793f9677f97add3e848ff149bccc6d285e2852ad6b0',
    }, {
        'fid': 5_784, 'source_record_id': '5784',
        'mapped_scale': '1:24,000', 'source_coordinate_count': 2,
        'source_native_length_m': 0.0203871998002148,
        'source_geometry_sha256':
            '9de8895cb60ce9739b715008903722cde25b00a33bae59f8d1fb547442744449',
        'state_clipped': False,
        'output_web_mercator_length_m': 0.02694925517472606,
        'output_geometry_sha256':
            '252836545c9495ee50472424ff5c75b416e989eecc7d2c89f6e494f2dd201746',
    }, {
        'fid': 16_553, 'source_record_id': '16553',
        'mapped_scale': '1:250,000', 'source_coordinate_count': 2,
        'source_native_length_m': 10.10351452668977,
        'source_geometry_sha256':
            '204e417f3bb4f0085a3063d2b33236f035152f165ee0e60aa66e82384e1af5d0',
        'state_clipped': True,
        'output_web_mercator_length_m': 0.1437026428689921,
        'output_geometry_sha256':
            'b59cd65a90ee41b996cb5e97ef9ad0306b390aa423e723b82959d2ed19779d33',
    }],
    'districts': [], 'umos': [],
}

for _encoding_records in ENCODING_EXCLUSIONS.values():
    for _encoding_record in _encoding_records:
        _encoding_record.update({
            'tippecanoe_maxzoom': 12,
            'tippecanoe_full_detail': 14,
            'z12_full_detail14_web_mercator_unit_m': 0.5971642834779395,
        })
        _encoding_record.setdefault(
            'review',
            'valid two-vertex output is shorter than one z12/full14 Web '
            'Mercator encoding unit and was absent from the complete maxzoom '
            'top-level-ID scan')

TIPPECANOE_VERSION = 'v2.79.0'
TIPPECANOE_MAXZOOM = 12
TIPPECANOE_FULL_DETAIL = 14
UT_BOUNDS = (-114.05287, 36.99766, -109.04157, 42.0017)
USER_AGENT = (
    'nw-mineral-monitor/11 Utah state-survey baseline builder '
    '(official public research data)')

ARCHIVE_NAMES = {
    'ugs-map179dm-500k.pmtiles': 'ut_ugs_map179dm_500k',
    'ugs-ds7-quaternary-faults.pmtiles': 'ut_ugs_ds7_quaternary_faults',
    'ugs-ofr695-mining-districts.pmtiles':
        'ut_ugs_ofr695_mining_districts',
    'ugs-ofr757-umos.pmtiles': 'ut_ugs_ofr757_umos',
}
ARCHIVE_ATTRIBUTIONS = {
    'ugs-map179dm-500k.pmtiles':
        'Utah Geological Survey Map 179DM; Hintze 1980; UGS/USGS',
    'ugs-ds7-quaternary-faults.pmtiles':
        'Utah Geological Survey Data Series 7; Hiscock 2026',
    'ugs-ofr695-mining-districts.pmtiles':
        'Utah Geological Survey Open-File Report 695; Krahulec 2018',
    'ugs-ofr757-umos.pmtiles':
        'Utah Geological Survey Open-File Report 757; Rupke 2023',
}

COMMON_PROVENANCE = (
    'fid', 'st', 'source_dataset', 'source_id', 'source_record_id',
    'source_scale', 'source_scale_status', 'source_ref', 'source_url',
    'publication_id')
LAYER_REQUIREMENTS = {
    'ut_ugs_map179dm_geology': [
        *COMMON_PROVENANCE, 'map_unit', 'unit_name', 'unit_age'],
    'ut_ugs_map179dm_structures': [
        *COMMON_PROVENANCE, 'feature_type', 'feature_subtype',
        'location_modifier'],
    'ut_ugs_ds7_quaternary_faults': [
        *COMMON_PROVENANCE, 'fault_age', 'mapped_scale',
        'mapping_constraint'],
    'ut_ugs_ofr695_mining_districts': [
        *COMMON_PROVENANCE, 'district_name', 'boundary_status'],
    'ut_ugs_ofr757_umos': [
        *COMMON_PROVENANCE, 'site_name', 'commodity', 'occurrence_scope'],
}


def _state_filter():
    return ['==', ['get', 'st'], 'UT']


BROWSER_LAYER_CONTRACTS = {
    'ut_ugs_map179dm_geology': {
        'title': 'Utah geology — UGS Map 179DM, 1:500,000',
        'geometry': 'polygon', 'activation_zoom': 5,
        'style': {
            'type': 'fill', 'filter': _state_filter(),
            'paint': {'fill-color': '#a88c67', 'fill-opacity': 0.25,
                      'fill-outline-color': '#6f604d'},
        },
        'semantic_note': (
            'Statewide 1:500,000 map units; not site-scale geology.'),
    },
    'ut_ugs_map179dm_structures': {
        'title': 'Utah contacts and structures — UGS Map 179DM',
        'geometry': 'line', 'activation_zoom': 6,
        'style': {
            'type': 'line', 'filter': _state_filter(),
            'paint': {
                'line-color': '#4a3b32', 'line-opacity': 0.72,
                'line-width': ['interpolate', ['linear'], ['zoom'],
                               6, 0.45, 12, 1.5],
            },
        },
        'semantic_note': (
            'Map contacts, faults, boundaries, veins, and marker beds; '
            'not an activity classification.'),
    },
    'ut_ugs_ds7_quaternary_faults': {
        'title': 'Utah Quaternary faults — UGS Data Series 7 (2026)',
        'geometry': 'line', 'activation_zoom': 5,
        'style': {
            'type': 'line', 'filter': _state_filter(),
            'paint': {
                'line-color': '#d94841', 'line-opacity': 0.88,
                'line-width': ['interpolate', ['linear'], ['zoom'],
                               5, 0.8, 12, 2.2],
            },
        },
        'semantic_note': (
            'Current UGS Quaternary compilation with per-trace mapped scale; '
            'a hazard/age classification, not a mineral-tenure layer.'),
    },
    'ut_ugs_ofr695_mining_districts': {
        'title': 'Utah historic mining districts — UGS OFR-695',
        'geometry': 'polygon', 'activation_zoom': 5,
        'style': {
            'type': 'fill', 'filter': _state_filter(),
            'paint': {'fill-color': '#d19a37', 'fill-opacity': 0.14,
                      'fill-outline-color': '#8a5b16'},
        },
        'semantic_note': (
            'Approximate historic district footprints; not tenure, title, '
            'or a mineral-resource boundary.'),
    },
    'ut_ugs_ofr757_umos': {
        'title': 'Utah Mineral Occurrence System — UGS OFR-757',
        'geometry': 'point', 'activation_zoom': 6,
        'style': {
            'type': 'circle', 'filter': _state_filter(),
            'paint': {
                'circle-color': '#d86cff',
                'circle-radius': ['interpolate', ['linear'], ['zoom'],
                                  6, 2, 12, 5],
                'circle-stroke-color': '#ffffff',
                'circle-stroke-width': 0.7, 'circle-opacity': 0.9,
            },
        },
        'semantic_note': (
            'Versioned occurrence/prospect/mine points; locations and stale '
            'historic attributes are not current land status.'),
    },
}

# Frozen, independently reviewed generation.  The public validator must not
# accept an archive merely because a replacement manifest is self-consistent:
# a tiny but structurally valid PMTiles set can otherwise declare its own
# counts, hashes, and source-ID inventory.  These constants bind the accepted
# official-source audit to the exact four future public bytes.
ACCEPTED_ARTIFACT_CONTRACTS = {
    'ut_ugs_map179dm_500k': {
        'n': 90_206, 'bytes': 59_081_057,
        'sha256':
            '5651c2118160db14a902117dd6baaf06ac0c342a4264cc1af35613e8586a029b',
        'bounds': [-114.05287, 36.997802, -109.042008, 42.000886],
        'semantic_tile_feature_counts': {
            'ut_ugs_map179dm_geology': 272_745,
            'ut_ugs_map179dm_structures': 849_519,
        },
        'maxzoom_unique_feature_ids': {
            'ut_ugs_map179dm_geology': 22_635,
            'ut_ugs_map179dm_structures': 67_571,
        },
    },
    'ut_ugs_ds7_quaternary_faults': {
        'n': 19_232, 'bytes': 8_245_068,
        'sha256':
            'ef10b8946f3b6134867abab068e2a61fb58c83ac1a7762347a77d44cc3c0f4d2',
        'bounds': [-114.049163, 37.000342, -109.042432, 42.00148],
        'semantic_tile_feature_counts': {
            'ut_ugs_ds7_quaternary_faults': 200_541,
        },
        'maxzoom_unique_feature_ids': {
            'ut_ugs_ds7_quaternary_faults': 19_232,
        },
    },
    'ut_ugs_ofr695_mining_districts': {
        'n': 185, 'bytes': 1_279_628,
        'sha256':
            'f67f043a89402b51e28a7e2cce54b92ab74fb6f06977726a40be562755836d3a',
        'bounds': [-114.050993, 36.99772, -109.04157, 41.9999589],
        'semantic_tile_feature_counts': {
            'ut_ugs_ofr695_mining_districts': 4_076,
        },
        'maxzoom_unique_feature_ids': {
            'ut_ugs_ofr695_mining_districts': 185,
        },
    },
    'ut_ugs_ofr757_umos': {
        'n': 7_787, 'bytes': 16_451_489,
        'sha256':
            'f1984b03f975a4e8241b88df9e52133efc7c95a9111797c92a9c0aee06b292e2',
        'bounds': [-114.048868, 36.998889, -109.043777, 41.991022],
        'semantic_tile_feature_counts': {
            'ut_ugs_ofr757_umos': 112_248,
        },
        'maxzoom_unique_feature_ids': {
            'ut_ugs_ofr757_umos': 7_787,
        },
    },
}

_ARCHIVE_BY_BASELINE = {
    'ut_ugs_map179dm_500k': 'map179',
    'ut_ugs_ds7_quaternary_faults': 'ds7',
    'ut_ugs_ofr695_mining_districts': 'districts',
    'ut_ugs_ofr757_umos': 'umos',
}

ACCEPTED_CLIP_CONTRACT = {
    'artifact': 'infra/state_clips.json',
    'artifact_sha256':
        '33c09d367d74a1ce0c88934d4adb548557733bf7da9105be039f5f16ed22c552',
    'authority': (
        'https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/'
        'State_County/MapServer/0 (States) and /1 (Counties), January 1 2025 '
        'vintage'),
    'method': 'geometric intersection',
}

ACCEPTED_RETRIEVED = '2026-08-13'
ACCEPTED_PROVENANCE_NOTE = (
    'Every feature retains Utah, publication, source-record, and '
    'source-scale context. This research baseline does not mark Utah DONE '
    'or establish mineral title.')
ACCEPTED_CATALOG_CONTRACTS = {
    'map179': {
        'title': 'Digital Geologic Map of Utah',
        'publication_id': 'UGS Map 179DM', 'scale': '1:500,000',
        'catalog_url': GEOLOGY_CATALOG, 'bulk_url': MAP179_URL,
    },
    'ds7': {
        'series_id': 'DS-7', 'pub_year': '2026',
        'pub_name': '2025 Update to the Utah Quaternary Fault Database',
        'pub_author': 'Adam I. Hiscock', 'pub_scale': None,
        'catalog_url': HAZARD_CATALOG, 'bulk_url': DS7_URL,
    },
    'districts': {
        'title': 'Utah Mining Districts',
        'publication_id': 'UGS Open-File Report 695',
        'scale': '1:1,000,000', 'catalog_url': MINERAL_CATALOG,
        'bulk_url': DISTRICTS_URL,
    },
    'umos': {
        'title': 'Utah Mineral Occurrence System',
        'publication_id': 'UGS Open-File Report 757',
        'catalog_url': MINERAL_CATALOG, 'bulk_url': UMOS_URL,
    },
}
ACCEPTED_DS7_COMPANION_EXCLUSIONS = {
    'UQD25_DS7_SSZ_full':
        'surface-fault-rupture special-study-zone polygons',
    'UQFD25_DS7_SSZnew':
        'new special-study-zone polygons already represented in full SSZ',
    'UQFD25_DS7_new':
        'new-line subset already represented in UQFD25_DS7_full',
}
ACCEPTED_UMOS_PROPERTY_EXCLUSIONS = {
    'OWNER, OPERATOR, LAND_STATUS': (
        'historic fields are not republished as current title or '
        'land-context claims; source archive remains checksum-bound'),
}


def _text(value, limit=2_000):
    if value is None:
        return None
    result = re.sub(r'\s+', ' ', str(value)).strip()
    return result[:limit] if result else None


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _canonical_sha256(value):
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(',', ':'), allow_nan=False,
        default=str).encode('utf-8')).hexdigest()


def _reject_json_constant(value):
    raise ValueError(f'non-standard JSON number {value}')


def _reject_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f'duplicate JSON object key {key!r}')
        result[key] = value
    return result


def _pm_decompress(data, compression):
    """Decode one bounded PMTiles member without a shared-validator import."""
    if compression == 1:
        return data
    if compression == 2:
        decoder = zlib.decompressobj(wbits=31)
        value = decoder.decompress(data, 256 * 1024 * 1024 + 1)
        if (len(value) > 256 * 1024 * 1024 or not decoder.eof or
                decoder.unused_data or decoder.unconsumed_tail):
            raise ValueError('PMTiles gzip member is oversized or malformed')
        return value
    raise ValueError(f'unsupported PMTiles internal compression {compression}')


def _pm_varint(data, position):
    value = 0
    shift = 0
    while position < len(data) and shift <= 63:
        byte = data[position]
        position += 1
        value |= (byte & 0x7f) << shift
        if byte < 0x80:
            return value, position
        shift += 7
    raise ValueError('malformed PMTiles/MVT varint')


def _pm_directory_entries(data):
    count, position = _pm_varint(data, 0)
    if count <= 0 or count > 10_000_000 or count > max(0, (len(data) - 1) // 4):
        raise ValueError(f'invalid PMTiles directory entry count {count}')
    tile_ids = []
    current = 0
    for index in range(count):
        delta, position = _pm_varint(data, position)
        if index and delta == 0:
            raise ValueError('PMTiles directory has duplicate tile IDs')
        current += delta
        tile_ids.append(current)
    run_lengths, lengths = [], []
    for target in (run_lengths, lengths):
        for _ in range(count):
            value, position = _pm_varint(data, position)
            target.append(value)
    offsets = []
    for index in range(count):
        value, position = _pm_varint(data, position)
        offset = ((offsets[index - 1] + lengths[index - 1])
                  if value == 0 and index else value - 1)
        if offset < 0 or lengths[index] <= 0:
            raise ValueError('invalid PMTiles directory offset/length')
        offsets.append(offset)
    if position != len(data):
        raise ValueError('PMTiles directory has trailing bytes')
    entries = list(zip(tile_ids, run_lengths, lengths, offsets))
    if any(index and entries[index - 1][0] >= row[0]
           for index, row in enumerate(entries)):
        raise ValueError('PMTiles directory is not strictly ordered')
    return entries


def _protobuf_fields(data):
    position = 0
    while position < len(data):
        tag, position = _pm_varint(data, position)
        field_number, wire_type = tag >> 3, tag & 7
        if field_number <= 0:
            raise ValueError('MVT protobuf contains field zero')
        if wire_type == 0:
            value, position = _pm_varint(data, position)
        elif wire_type == 1:
            if position + 8 > len(data):
                raise ValueError('truncated MVT fixed64 field')
            value, position = data[position:position + 8], position + 8
        elif wire_type == 2:
            length, position = _pm_varint(data, position)
            if position + length > len(data):
                raise ValueError('truncated MVT length-delimited field')
            value, position = data[position:position + length], position + length
        elif wire_type == 5:
            if position + 4 > len(data):
                raise ValueError('truncated MVT fixed32 field')
            value, position = data[position:position + 4], position + 4
        else:
            raise ValueError(f'unsupported MVT protobuf wire type {wire_type}')
        yield field_number, wire_type, value


def _packed_varints(data, label):
    values = []
    position = 0
    while position < len(data):
        try:
            value, position = _pm_varint(data, position)
        except ValueError as exc:
            raise ValueError(f'malformed MVT {label}') from exc
        values.append(value)
    return values


def _mvt_value(data):
    values = []
    for field, wire, value in _protobuf_fields(data):
        if field == 1 and wire == 2:
            try:
                values.append(value.decode('utf-8'))
            except UnicodeDecodeError as exc:
                raise ValueError('MVT string value is not UTF-8') from exc
        elif field == 2 and wire == 5:
            values.append(struct.unpack('<f', value)[0])
        elif field == 3 and wire == 1:
            values.append(struct.unpack('<d', value)[0])
        elif field in (4, 5) and wire == 0:
            values.append(value)
        elif field == 6 and wire == 0:
            values.append((value >> 1) ^ -(value & 1))
        elif field == 7 and wire == 0 and value in (0, 1):
            values.append(bool(value))
    if len(values) != 1:
        raise ValueError('MVT property value must contain one typed value')
    if isinstance(values[0], float) and not math.isfinite(values[0]):
        raise ValueError('MVT property value is not finite')
    return values[0]


def _validate_mvt_geometry(values):
    if not values:
        raise ValueError('MVT feature has empty geometry')
    position = 0
    while position < len(values):
        command = values[position]
        position += 1
        command_id, count = command & 7, command >> 3
        if count <= 0 or command_id not in (1, 2, 7):
            raise ValueError('MVT feature has an invalid geometry command')
        parameters = 2 * count if command_id in (1, 2) else 0
        if position + parameters > len(values):
            raise ValueError('MVT feature geometry command is truncated')
        position += parameters


def _mvt_feature(data, keys, values):
    ids, tags, geometries, geometry_types = [], [], [], []
    for field, wire, value in _protobuf_fields(data):
        if field == 1 and wire == 0:
            ids.append(value)
        elif field == 2 and wire == 2:
            tags.extend(_packed_varints(value, 'feature tags'))
        elif field == 2 and wire == 0:
            tags.append(value)
        elif field == 3 and wire == 0:
            geometry_types.append(value)
        elif field == 4 and wire == 2:
            geometries.extend(_packed_varints(value, 'feature geometry'))
        elif field == 4 and wire == 0:
            geometries.append(value)
    if len(tags) % 2:
        raise ValueError('MVT feature tags are not key/value pairs')
    properties = {}
    for key_index, value_index in zip(tags[::2], tags[1::2]):
        if key_index >= len(keys) or value_index >= len(values):
            raise ValueError('MVT feature tag index is outside its dictionary')
        key = keys[key_index]
        if key in properties:
            raise ValueError(f'MVT feature repeats property {key!r}')
        properties[key] = values[value_index]
    if len(geometry_types) != 1 or geometry_types[0] not in (1, 2, 3):
        raise ValueError('MVT feature lacks a valid geometry type')
    _validate_mvt_geometry(geometries)
    if len(ids) > 1:
        raise ValueError('MVT feature repeats its feature ID')
    return {'id': ids[0] if ids else None, 'properties': properties,
            'geometry_type': geometry_types[0]}


def _mvt_layers(data, semantic=False):
    layers = []
    for field, wire, raw_layer in _protobuf_fields(data):
        if field != 3 or wire != 2:
            continue
        names, versions, extents = [], [], []
        raw_features, keys, raw_values = [], [], []
        for number, item_wire, value in _protobuf_fields(raw_layer):
            if number == 1 and item_wire == 2:
                try:
                    names.append(value.decode('utf-8'))
                except UnicodeDecodeError as exc:
                    raise ValueError('MVT layer name is not UTF-8') from exc
            elif number == 2 and item_wire == 2:
                list(_protobuf_fields(value))
                raw_features.append(value)
            elif number == 3 and item_wire == 2:
                try:
                    keys.append(value.decode('utf-8'))
                except UnicodeDecodeError as exc:
                    raise ValueError('MVT property key is not UTF-8') from exc
            elif number == 4 and item_wire == 2:
                raw_values.append(value)
            elif number == 5 and item_wire == 0:
                extents.append(value)
            elif number == 15 and item_wire == 0:
                versions.append(value)
        if len(names) != 1 or not names[0] or not raw_features:
            raise ValueError('MVT layer needs one name and at least one feature')
        if versions and (len(versions) != 1 or versions[0] not in (1, 2)):
            raise ValueError('MVT layer declares an invalid version')
        if extents and (len(extents) != 1 or not 1 <= extents[0] <= 2 ** 24):
            raise ValueError('MVT layer declares an invalid extent')
        if len(keys) != len(set(keys)) or any(not key for key in keys):
            raise ValueError('MVT layer property keys are empty or duplicated')
        values = [_mvt_value(value) for value in raw_values] if semantic else []
        features = ([_mvt_feature(value, keys, values)
                     for value in raw_features] if semantic else [])
        layers.append({'name': names[0], 'features': features,
                       'feature_count': len(raw_features)})
    names = [layer['name'] for layer in layers]
    if not layers or len(names) != len(set(names)):
        raise ValueError('MVT tile has no layers or duplicate layer names')
    return layers


def _strict_pmtiles_header(path, expected_layers=None, required_properties=None,
                           *, verify_feature_properties=False,
                           expected_state=None, expected_bounds=None,
                           collect_feature_ids=False, **unsupported):
    """Strict self-contained PMTiles v3/MVT scanner for private Utah builds.

    The shared validator may inject its own scanner into
    ``validate_manifest_baselines``.  Keeping this private-build scanner local
    avoids coupling source audits to concurrent validator transactions.
    """
    if unsupported:
        raise ValueError(
            f'unsupported Utah PMTiles scan options: {sorted(unsupported)}')
    if collect_feature_ids and not verify_feature_properties:
        raise ValueError('feature IDs require a full semantic PMTiles scan')
    with open(path, 'rb') as source:
        head = source.read(127)
    if len(head) != 127 or head[:7] != b'PMTiles' or head[7] != 3:
        raise ValueError('bad PMTiles v3 magic/header')
    size = os.path.getsize(path)
    (root_offset, root_length, metadata_offset, metadata_length,
     leaf_offset, leaf_length, tile_offset, tile_length,
     addressed, entries, contents) = struct.unpack_from('<11Q', head, 8)
    ranges = [(root_offset, root_length, 'root directory'),
              (metadata_offset, metadata_length, 'metadata'),
              (tile_offset, tile_length, 'tile data')]
    if leaf_length:
        ranges.append((leaf_offset, leaf_length, 'leaf directory'))
    for offset, length, label in ranges:
        if offset < 127 or length <= 0 or offset + length > size:
            raise ValueError(f'invalid PMTiles {label} range')
    ordered = sorted((offset, offset + length, label)
                     for offset, length, label in ranges)
    if any(left[1] > right[0] for left, right in zip(ordered, ordered[1:])):
        raise ValueError('PMTiles archive ranges overlap')
    if min(addressed, entries, contents) <= 0:
        raise ValueError('PMTiles archive declares no tiles')
    clustered, internal_compression, tile_compression, tile_type = head[96:100]
    if clustered not in (0, 1) or internal_compression not in (1, 2):
        raise ValueError('PMTiles header flags/compression are invalid')
    if tile_compression not in (1, 2) or tile_type != 1:
        raise ValueError('PMTiles artifact is not a supported MVT archive')
    minzoom, maxzoom = head[100], head[101]
    if minzoom > maxzoom or maxzoom > 24:
        raise ValueError('PMTiles zoom range is invalid')
    bounds = [value / 10_000_000
              for value in struct.unpack_from('<4i', head, 102)]
    if not (-180 <= bounds[0] < bounds[2] <= 180 and
            -90 <= bounds[1] < bounds[3] <= 90):
        raise ValueError('PMTiles bounds are invalid')
    with open(path, 'rb') as source:
        source.seek(root_offset)
        root = _pm_decompress(source.read(root_length), internal_compression)
        source.seek(metadata_offset)
        metadata_bytes = _pm_decompress(
            source.read(metadata_length), internal_compression)
        root_directory = _pm_directory_entries(root)
        tile_entries, leaf_pointers = [], []
        for tile_id, run_length, length, offset in root_directory:
            if run_length:
                tile_entries.append((tile_id, run_length, length, offset))
            else:
                if not leaf_length or offset + length > leaf_length:
                    raise ValueError('PMTiles root points outside leaf directory')
                leaf_pointers.append((tile_id, offset, length))
        leaf_ranges = sorted({(offset, length)
                              for _, offset, length in leaf_pointers})
        if any(left[0] + left[1] > right[0]
               for left, right in zip(leaf_ranges, leaf_ranges[1:])):
            raise ValueError('PMTiles leaf directory ranges overlap')
        decoded_leaves = {}
        for offset, length in leaf_ranges:
            source.seek(leaf_offset + offset)
            decoded = _pm_directory_entries(_pm_decompress(
                source.read(length), internal_compression))
            if any(run_length == 0 for _, run_length, _, _ in decoded):
                raise ValueError('PMTiles leaf contains another leaf pointer')
            decoded_leaves[(offset, length)] = decoded
            tile_entries.extend(decoded)
        for tile_id, offset, length in leaf_pointers:
            if decoded_leaves[(offset, length)][0][0] != tile_id:
                raise ValueError('PMTiles leaf pointer tile ID is inconsistent')
    tile_entries.sort()
    max_tile_id = (4 ** (maxzoom + 1) - 1) // 3
    for index, (tile_id, run_length, length, offset) in enumerate(tile_entries):
        if (offset + length > tile_length or tile_id + run_length > max_tile_id or
                (index and tile_entries[index - 1][0] +
                 tile_entries[index - 1][1] > tile_id)):
            raise ValueError('PMTiles tile directory is invalid')
    if (len(tile_entries) != entries or
            sum(row[1] for row in tile_entries) != addressed or
            len({(row[3], row[2]) for row in tile_entries}) != contents):
        raise ValueError('PMTiles header counts do not match directories')
    try:
        metadata = json.loads(
            metadata_bytes, parse_constant=_reject_json_constant,
            object_pairs_hook=_reject_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f'invalid PMTiles JSON metadata: {exc}') from exc
    vector_layers = metadata.get('vector_layers') if isinstance(metadata, dict) else None
    if (not isinstance(vector_layers, list) or not vector_layers or
            any(not isinstance(item, dict) or
                not isinstance(item.get('id'), str) or not item['id'] or
                not isinstance(item.get('fields'), dict) or
                any(not isinstance(key, str) or not key or
                    not isinstance(value, str)
                    for key, value in item.get('fields', {}).items())
                for item in vector_layers)):
        raise ValueError('PMTiles vector layer metadata schema is invalid')
    layer_metadata = {item['id']: item for item in vector_layers}
    if len(layer_metadata) != len(vector_layers):
        raise ValueError('PMTiles metadata has duplicate vector layers')
    expected = set(expected_layers or [])
    if any(not isinstance(layer, str) or not layer for layer in expected):
        raise ValueError('expected PMTiles layers must be nonempty strings')
    if expected and not expected <= set(layer_metadata):
        raise ValueError('PMTiles metadata lacks an expected source layer')
    if isinstance(required_properties, dict):
        requirements = {layer: set(fields)
                        for layer, fields in required_properties.items()}
    else:
        requirements = {layer: set(required_properties or [])
                        for layer in expected}
    for layer, fields in requirements.items():
        if layer not in expected or any(not isinstance(field, str) or not field
                                        for field in fields):
            raise ValueError('required PMTiles property contract is invalid')
        missing = fields - set(layer_metadata[layer]['fields'])
        if missing:
            raise ValueError(
                f'PMTiles source layer {layer} lacks metadata fields {sorted(missing)}')
    if expected_bounds is not None and not any(
            bbox[0] <= bounds[2] and bbox[2] >= bounds[0] and
            bbox[1] <= bounds[3] and bbox[3] >= bounds[1]
            for bbox in expected_bounds):
        raise ValueError('PMTiles bounds do not intersect expected bounds')
    semantic_counts, maxzoom_counts, maxzoom_ids, sampled_layers = {}, {}, {}, set()
    content_ranges = sorted({(row[3], row[2]) for row in tile_entries})
    maxzoom_first = (4 ** maxzoom - 1) // 3
    maxzoom_after = (4 ** (maxzoom + 1) - 1) // 3
    maxzoom_ranges = {
        (offset, length) for tile_id, run_length, length, offset in tile_entries
        if tile_id < maxzoom_after and tile_id + run_length > maxzoom_first}
    with open(path, 'rb') as source:
        for offset, length in content_ranges:
            source.seek(tile_offset + offset)
            payload = source.read(length)
            if len(payload) != length:
                raise ValueError('PMTiles tile payload is truncated')
            decoded_layers = _mvt_layers(
                _pm_decompress(payload, tile_compression),
                semantic=verify_feature_properties)
            for layer in decoded_layers:
                name = layer['name']
                sampled_layers.add(name)
                semantic_counts[name] = (
                    semantic_counts.get(name, 0) + layer['feature_count'])
                at_maxzoom = (offset, length) in maxzoom_ranges
                if at_maxzoom:
                    maxzoom_counts[name] = (
                        maxzoom_counts.get(name, 0) + layer['feature_count'])
                if not verify_feature_properties:
                    continue
                for feature in layer['features']:
                    missing = requirements.get(name, set()) - set(feature['properties'])
                    if missing:
                        raise ValueError(
                            f'PMTiles source layer {name} feature lacks {sorted(missing)}')
                    if (expected_state is not None and
                            feature['properties'].get('st') != expected_state):
                        raise ValueError(
                            f'PMTiles source layer {name} feature has wrong state')
                    if collect_feature_ids and at_maxzoom and name in expected:
                        feature_id = feature['id']
                        if (not isinstance(feature_id, int) or
                                isinstance(feature_id, bool) or feature_id < 0):
                            raise ValueError(
                                f'PMTiles source layer {name} lacks a valid top-level ID')
                        maxzoom_ids.setdefault(name, set()).add(feature_id)
    if not sampled_layers <= set(layer_metadata):
        raise ValueError('MVT layer is absent from PMTiles metadata')
    return {
        'version': 3, 'bytes': size, 'bounds': bounds,
        'minzoom': minzoom, 'maxzoom': maxzoom,
        'source_layers': sorted(layer_metadata),
        'root_entries': len(root_directory), 'sample_layers': sorted(sampled_layers),
        'tile_entries': entries, 'tile_contents': contents,
        'description': metadata.get('description'),
        'semantic_layer_counts': semantic_counts,
        'all_zoom_feature_instances': semantic_counts,
        'maxzoom_feature_instances': maxzoom_counts,
        'maxzoom_feature_ids': {
            layer: sorted(maxzoom_ids.get(layer, set()))
            for layer in sorted(expected)} if collect_feature_ids else {},
        'field_types': {
            layer: dict(layer_metadata[layer]['fields'])
            for layer in sorted(layer_metadata)},
    }


def _ensure_private_staging_root():
    site = os.path.realpath(SITE)
    staging = os.path.realpath(PRIVATE_STAGING_ROOT)
    if os.path.commonpath((site, staging)) == site:
        raise RuntimeError('Utah staging root must remain outside site/')
    os.makedirs(staging, exist_ok=True)
    return staging


def _request_bytes(url, tries=6):
    last = None
    for attempt in range(tries):
        try:
            request = urllib.request.Request(
                url, headers={'User-Agent': USER_AGENT})
            with urllib.request.urlopen(request, timeout=300) as response:
                return response.read()
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last = exc
        if attempt + 1 < tries:
            time.sleep(min(30, 2 ** attempt))
    raise RuntimeError(f'official Utah request failed: {url}: {last}')


def _verify_authority_catalogs():
    geology = _request_bytes(GEOLOGY_CATALOG).decode('utf-8', 'replace')
    minerals = _request_bytes(MINERAL_CATALOG).decode('utf-8', 'replace')
    try:
        hazards = json.loads(_request_bytes(HAZARD_CATALOG))
    except json.JSONDecodeError as exc:
        raise RuntimeError('UGS hazard catalog is not JSON') from exc
    if ('Digital Geologic Map of Utah' not in geology or
            'GeologicMapOfUtah.zip' not in geology):
        raise RuntimeError('UGS Map 179DM catalog identity changed')
    if ('Utah Mineral Occurrence System' not in minerals or
            'ofr-757.zip' not in minerals or
            'Mining_Districts_20190116gdb.zip' not in minerals):
        raise RuntimeError('UGS mineral catalog identity changed')
    rows = [row for row in hazards if isinstance(row, dict) and
            row.get('series_id') == 'DS-7']
    if len(rows) != 1:
        raise RuntimeError('UGS DS-7 catalog row is missing or duplicated')
    row = rows[0]
    selected = {field: row.get(field) for field in (
        'series_id', 'pub_year', 'pub_name', 'pub_author', 'pub_scale')}
    expected = {
        'series_id': 'DS-7', 'pub_year': '2026',
        'pub_name': '2025 Update to the Utah Quaternary Fault Database',
        'pub_author': 'Adam I. Hiscock', 'pub_scale': None,
    }
    if selected != expected or DS7_URL not in str(row.get('dLpopOver')):
        raise RuntimeError(f'UGS DS-7 catalog identity changed: {selected}')
    return {
        'map179': {
            'title': 'Digital Geologic Map of Utah',
            'publication_id': 'UGS Map 179DM', 'scale': '1:500,000',
            'catalog_url': GEOLOGY_CATALOG, 'bulk_url': MAP179_URL,
        },
        'ds7': {**selected, 'catalog_url': HAZARD_CATALOG,
                'bulk_url': DS7_URL},
        'districts': {
            'title': 'Utah Mining Districts',
            'publication_id': 'UGS Open-File Report 695',
            'scale': '1:1,000,000', 'catalog_url': MINERAL_CATALOG,
            'bulk_url': DISTRICTS_URL,
        },
        'umos': {
            'title': 'Utah Mineral Occurrence System',
            'publication_id': 'UGS Open-File Report 757',
            'catalog_url': MINERAL_CATALOG, 'bulk_url': UMOS_URL,
        },
    }


def _archive_inventory(path):
    try:
        with zipfile.ZipFile(path) as archive:
            return [{
                'name': item.filename, 'bytes': item.file_size,
                'compressed_bytes': item.compress_size,
                'crc32': f'{item.CRC:08x}', 'is_dir': item.is_dir(),
            } for item in archive.infolist()]
    except (OSError, zipfile.BadZipFile) as exc:
        raise RuntimeError(f'invalid Utah source ZIP {path}: {exc}') from exc


def _download_archive(key, path):
    contract = ARCHIVE_CONTRACTS[key]
    payload = _request_bytes(contract['url'])
    with open(path, 'wb') as output:
        output.write(payload)
        output.flush()
        os.fsync(output.fileno())
    observed = {'bytes': os.path.getsize(path), 'sha256': _sha256(path)}
    expected = {'bytes': contract['bytes'], 'sha256': contract['sha256']}
    if observed != expected:
        raise RuntimeError(
            f'{key} official archive identity changed: {observed} != {expected}')
    inventory = _archive_inventory(path)
    if (len(inventory) != contract['member_count'] or
            _canonical_sha256(inventory) !=
            contract['member_inventory_sha256']):
        raise RuntimeError(f'{key} archive member inventory changed')
    return {**observed, 'member_count': len(inventory),
            'member_inventory_sha256': _canonical_sha256(inventory)}


def _safe_extract_archive(key, path, destination):
    """Extract a checksum-pinned ZIP without traversal or symlink entries."""
    os.makedirs(destination, exist_ok=False)
    root = os.path.realpath(destination)
    with zipfile.ZipFile(path) as archive:
        for item in archive.infolist():
            target = os.path.realpath(os.path.join(root, item.filename))
            if os.path.commonpath((root, target)) != root:
                raise RuntimeError(f'{key} ZIP contains an unsafe path')
            mode = (item.external_attr >> 16) & 0o170000
            if mode == stat.S_IFLNK:
                raise RuntimeError(f'{key} ZIP contains a symlink')
        archive.extractall(root)
    return root


def _prepare_sources(temp):
    downloads, roots = {}, {}
    for key, contract in ARCHIVE_CONTRACTS.items():
        path = os.path.join(temp, contract['filename'])
        downloads[key] = _download_archive(key, path)
        if contract['extract']:
            extracted = _safe_extract_archive(
                key, path, os.path.join(temp, f'{key}-source'))
            roots[key] = os.path.join(extracted, contract['gdb'])
        else:
            roots[key] = 'zip://' + os.path.abspath(path)
        layers = tuple(fiona.listlayers(roots[key]))
        if layers != contract['layers']:
            raise RuntimeError(
                f'{key} source-layer inventory changed: {layers}')
    return roots, downloads


def _source_layer_manifest(source, layer):
    with fiona.open(source, layer=layer) as rows:
        return {
            'driver': rows.driver,
            'crs': rows.crs.to_string() if rows.crs else None,
            'schema': rows.schema,
            'bounds': list(rows.bounds), 'n': len(rows),
        }


def _verify_source_layers(roots):
    result = {}
    for key, spec in SOURCE_SPECS.items():
        source = roots[spec['archive']]
        manifest = _source_layer_manifest(source, spec['layer'])
        manifest_sha = _canonical_sha256(manifest)
        with fiona.open(source, layer=spec['layer']) as rows:
            source_fids = [int(row.id) for row in rows]
        if (manifest_sha != spec['manifest_sha256'] or
                _canonical_sha256(source_fids) !=
                spec['source_fids_sha256']):
            raise RuntimeError(
                f'{key} typed source layer/schema/FID inventory changed')
        result[key] = {
            **manifest, 'manifest_sha256': manifest_sha,
            'source_fids_sha256': _canonical_sha256(source_fids),
            'minimum_source_fid': min(source_fids),
            'maximum_source_fid': max(source_fids),
        }
    return result


def _load_ut_clip():
    with open(STATE_CLIPS, encoding='utf-8') as source:
        document = json.load(source)
    if (document.get('schema_version') != 1 or
            len(document.get('states') or {}) != 49 or
            'TIGERweb' not in str(document.get('source') or '')):
        raise RuntimeError('authoritative state clip index is invalid')
    boundary = shapely_shape(document['states']['UT'])
    if (boundary.geom_type != 'Polygon' or boundary.is_empty or
            not boundary.is_valid or tuple(boundary.bounds) != UT_BOUNDS):
        raise RuntimeError('authoritative Utah boundary is invalid')
    return {
        'boundary': boundary, 'prepared': shapely_prepare(boundary),
        'manifest': {
            'artifact': os.path.relpath(STATE_CLIPS, ROOT),
            'artifact_sha256': _sha256(STATE_CLIPS),
            'authority': document['source'],
            'method': 'geometric intersection',
        },
    }


def _atomic_parts(geometry, kind):
    wanted = {'polygon': 'Polygon', 'line': 'LineString', 'point': 'Point'}[kind]
    if geometry.geom_type == wanted:
        return [geometry]
    if geometry.geom_type in (
            'MultiPolygon', 'MultiLineString', 'MultiPoint',
            'GeometryCollection'):
        result = []
        for part in geometry.geoms:
            result.extend(_atomic_parts(part, kind))
        return result
    return []


def _same_dimension(geometry, kind):
    parts = [part for part in _atomic_parts(geometry, kind)
             if not part.is_empty and
             (part.area > 0 if kind == 'polygon' else
              part.length > 0 if kind == 'line' else True)]
    if not parts:
        return None
    result = shapely_unary_union(parts)
    allowed = {
        'polygon': ('Polygon', 'MultiPolygon'),
        'line': ('LineString', 'MultiLineString'),
        'point': ('Point', 'MultiPoint'),
    }[kind]
    if result.is_empty or result.geom_type not in allowed or not result.is_valid:
        raise RuntimeError(
            f'{kind} extraction produced invalid {result.geom_type!r}')
    return result


def _repair_polygon(geometry, fid):
    if geometry.is_valid:
        return geometry, None
    reason = shapely_explain_validity(geometry)
    repaired = shapely_make_valid(geometry)
    output = _same_dimension(repaired, 'polygon')
    if output is None:
        raise RuntimeError(f'polygon {fid} repair has no polygon output')
    source_area, output_area = float(geometry.area), float(output.area)
    absolute = abs(output_area - source_area)
    relative = absolute / source_area if source_area else math.inf
    if not math.isfinite(relative) or absolute > 1e-5 or relative > 1e-12:
        raise RuntimeError(f'polygon {fid} repair exceeds lossless tolerance')
    return output, {
        'fid': fid, 'validity_reason': reason,
        'validity_reason_class': reason.split('[', 1)[0],
        'source_type': geometry.geom_type,
        'make_valid_type': repaired.geom_type,
        'polygon_output_type': output.geom_type,
        'source_area': source_area, 'repaired_area': output_area,
        'absolute_area_delta': absolute, 'relative_area_delta': relative,
    }


def _base_properties(fid, source_record_id, *, dataset, source_id, scale,
                     scale_status, source_ref, source_url, publication_id):
    return {
        'fid': fid, 'st': 'UT', 'source_dataset': dataset,
        'source_id': source_id, 'source_record_id': str(source_record_id),
        'source_scale': scale, 'source_scale_status': scale_status,
        'source_ref': source_ref, 'source_url': source_url,
        'publication_id': publication_id,
    }


def _normalize_properties(key, fid, source_record_id, raw):
    if key in ('geology_lines', 'geology_units'):
        result = _base_properties(
            fid, source_record_id, dataset='ugs_map179dm_geologic_map_utah',
            source_id=f'ugs-map179dm:{key}:{source_record_id}',
            scale='1:500,000', scale_status='publication_fixed_scale',
            source_ref='UGS Map 179DM / Hintze (1980)',
            source_url=MAP179_URL, publication_id='UGS Map 179DM')
        if key == 'geology_units':
            result.update({
                'map_unit': _text(raw.get('UNITSYMBOL'), 100) or 'unknown',
                'unit_name': _text(raw.get('UNITNAME'), 500) or 'unknown',
                'unit_age': _text(raw.get('AGE'), 200) or 'unknown',
                'notes': _text(raw.get('NOTES'), 500),
                'source_geology_id': raw.get('GEOLOGY_ID'),
            })
        else:
            result.update({
                'feature_type': _text(raw.get('TYPE'), 200) or 'unknown',
                'feature_subtype': _text(raw.get('SUBTYPE'), 200) or 'none',
                'location_modifier': _text(raw.get('MODIFIER'), 200) or 'none',
                'fault_connection': _text(raw.get('FAULT_CON'), 100),
                'notes': _text(raw.get('NOTES'), 500),
                'source_geology_id': raw.get('GEOLOGY_ID'),
            })
    elif key == 'faults':
        mapped_scale = _text(raw.get('MappedScale'), 50)
        if mapped_scale is None or not re.fullmatch(r'1:[0-9,]+', mapped_scale):
            raise RuntimeError(
                f'DS-7 feature {fid} lacks a typed mapped scale')
        result = _base_properties(
            fid, source_record_id, dataset='ugs_ds7_quaternary_fault_database',
            source_id=f'ugs-ds7:fault:{source_record_id}',
            scale=mapped_scale, scale_status='per_feature_mapped_scale',
            source_ref='UGS Data Series 7 / Hiscock (2026)',
            source_url=DS7_URL, publication_id='UGS Data Series 7')
        result.update({
            'mapped_scale': mapped_scale,
            'fault_number': _text(raw.get('FaultNum'), 100),
            'fault_zone': _text(raw.get('FaultZone'), 200),
            'fault_name': _text(raw.get('FaultName'), 300),
            'section_name': _text(raw.get('SectionName'), 300),
            'strand_name': _text(raw.get('StrandName'), 300),
            'fault_age': _text(raw.get('FaultAge'), 100) or 'unknown',
            'fault_class': _text(raw.get('FaultClass'), 100),
            'mapping_constraint': (
                _text(raw.get('MappingConstraint'), 200) or 'unknown'),
            'dip_direction': _text(raw.get('DipDirection'), 100),
            'slip_sense': _text(raw.get('SlipSense'), 100),
            'slip_rate': _text(raw.get('SlipRate'), 100),
            'label': _text(raw.get('Label'), 300),
            'summary': _text(raw.get('Summary'), 2_000),
            'citation': _text(raw.get('Citation'), 1_000),
            'citation_url': _text(raw.get('Citation_Link'), 500),
            'usgs_url': _text(raw.get('USGS_Link'), 500),
            'source_has_z': 1,
        })
    elif key == 'districts':
        result = _base_properties(
            fid, source_record_id, dataset='ugs_ofr695_mining_districts',
            source_id=f'ugs-ofr695:district:{source_record_id}',
            scale='1:1,000,000', scale_status='publication_map_scale',
            source_ref='UGS Open-File Report 695 / Krahulec (2018)',
            source_url=DISTRICTS_URL,
            publication_id='UGS Open-File Report 695')
        result.update({
            'district_name': _text(raw.get('District'), 500) or 'unknown',
            'synonym': _text(raw.get('Synonym'), 500),
            'commodity': _text(raw.get('Commodity'), 500),
            'organized_year': raw.get('Organized'),
            'productive_years': _text(raw.get('Productive'), 200),
            'short_tons': raw.get('Short_Tons'),
            'historic_total_dollar_value': raw.get('Total_Dollar_Value'),
            'reviewed': _text(raw.get('Reviewed'), 50),
            'boundary_status': (
                'approximate historic district footprint; not tenure or title'),
        })
    elif key == 'umos':
        result = _base_properties(
            fid, source_record_id, dataset='ugs_ofr757_umos',
            source_id=f'ugs-ofr757:umos:{source_record_id}',
            scale='N/A (point record)',
            scale_status='point_location_precision_not_encoded_per_record',
            source_ref='UGS Open-File Report 757 / Rupke (2023)',
            source_url=UMOS_URL,
            publication_id='UGS Open-File Report 757')
        result.update({
            'site_name': _text(raw.get('SITE_NAME'), 500) or 'unnamed',
            'synonym': _text(raw.get('SYNONYM'), 500),
            'district_name': _text(raw.get('DISTRICT'), 300),
            'commodity': _text(raw.get('COMMODITY'), 500) or 'unknown',
            'occurrence_scope': 'mine/prospect/occurrence/deposit record',
            'record_type': _text(raw.get('TYPE'), 300),
            'mineralization_age_ma': raw.get('AGE_MA'),
            'deposit_model': _text(raw.get('DEP_MODEL'), 500),
            'production_class': _text(raw.get('PRODUCTION'), 200),
            'deposit_size': _text(raw.get('DEP_SIZE'), 200),
            'ore_minerals': _text(raw.get('ORE_MINERALS'), 1_000),
            'alteration': _text(raw.get('ALTERATION'), 1_000),
            'major_commodities': _text(raw.get('MAJOR'), 500),
            'minor_commodities': _text(raw.get('MINOR'), 500),
            'occurrence_commodities': _text(raw.get('OCCURRENCE'), 500),
            'deposit_type': _text(raw.get('DEP_TYPE'), 500),
            'deposit_form': _text(raw.get('DEP_FORM'), 500),
            'deposit_description': _text(raw.get('DEP_DESCRIPTION'), 1_500),
            'status': _text(raw.get('STATUS'), 500),
            'workings': _text(raw.get('WORKINGS'), 500),
            'work_depth_ft': raw.get('WORK_DEPTH_FT'),
            'local_structure': _text(raw.get('LOCAL_STRUCTURE'), 1_000),
            'host_rock_age': _text(raw.get('HR_AGE'), 300),
            'host_rock_formation': _text(raw.get('HR_FORMATION'), 1_000),
            'host_rock_type': _text(raw.get('HR_TYPE'), 1_000),
            'igneous_age': _text(raw.get('IGNEOUS_AGE'), 300),
            'igneous_name': _text(raw.get('IGNEOUS_NAME'), 500),
            'igneous_type': _text(raw.get('IGNEOUS_TYPE'), 500),
            'mineralization_age': _text(raw.get('MINERAL_AGE'), 300),
            'ore_control': _text(raw.get('ORE_CONTROL'), 1_000),
            'geology_comments': _text(raw.get('GEOLOGY_COM'), 1_500),
            'tectonic_setting': _text(raw.get('TECTONIC_SETTING'), 500),
            'regional_structure': _text(raw.get('REGIONAL_STRUCTURE'), 1_000),
            'county': _text(raw.get('COUNTY'), 200),
            'quadrangle_24000': _text(raw.get('QUADRANGLE_24000'), 300),
            'agency': _text(raw.get('AGENCY'), 200),
            'record_date': _text(raw.get('DATE'), 100),
            'reference_1': _text(raw.get('REFERENCE1'), 1_500),
            'reference_2': _text(raw.get('REFERENCE2'), 1_500),
            'reference_3': _text(raw.get('REFERENCE3'), 1_500),
            'reference_4': _text(raw.get('REFERENCE4'), 1_500),
        })
    else:  # pragma: no cover - SOURCE_SPECS is closed
        raise RuntimeError(f'unknown Utah source {key}')
    return result


def _write_feature(output, feature):
    output.write(json.dumps(
        feature, sort_keys=True, separators=(',', ':'), allow_nan=False))
    output.write('\n')


def _repair_evidence(records):
    reasons = Counter(row['validity_reason_class'] for row in records)
    transitions = Counter('->'.join((
        row['source_type'], row['make_valid_type'],
        row['polygon_output_type'])) for row in records)
    return {
        'status': ('reviewed_pinned_source_repair' if records else
                   'reviewed_pinned_source_no_repair_required'),
        'ordering': (
            'validate_then_make_valid_in_native_crs_then_epsg4326_transform_'
            'then_state_intersection'),
        'method': ('GEOSMakeValid via shapely.make_valid' if records else None),
        'shapely_version': getattr(shapely, '__version__', None),
        'geos_version': getattr(shapely, 'geos_version_string', None),
        'count': len(records), 'fids': [row['fid'] for row in records],
        'fids_sha256': _canonical_sha256([row['fid'] for row in records]),
        'validity_reason_counts': dict(sorted(reasons.items())),
        'type_transition_counts': dict(sorted(transitions.items())),
        'records': records, 'records_sha256': _canonical_sha256(records),
        'area_delta': {
            'units': 'native CRS square units',
            'maximum_absolute': max(
                (row['absolute_area_delta'] for row in records), default=0.0),
            'maximum_relative': max(
                (row['relative_area_delta'] for row in records), default=0.0),
            'sum_absolute': sum(
                row['absolute_area_delta'] for row in records),
        },
    }


def _assert_geometry_contract(key, stats):
    contract = GEOMETRY_CONTRACTS[key]
    observed = {
        'source_records': stats['source_records'],
        'source_types': stats['source_geometry_types'],
        'source_object_ids_sha256': stats['source_object_ids_sha256'],
        'output_types': stats['tiled_geometry_types'],
        'empty_count': stats['empty_geometry_count'],
        'empty_sha256': stats['empty_geometry_fids_sha256'],
        'unusable_count': stats['unusable_source_geometry']['count'],
        'unusable_sha256': stats['unusable_source_geometry']['fids_sha256'],
        'outside_count': stats['spatial_clip']['fully_outside_count'],
        'outside_sha256': stats['spatial_clip'][
            'fully_outside_fids_sha256'],
        'clipped_count': stats['spatial_clip']['geometry_clipped_count'],
        'clipped_sha256': stats['spatial_clip'][
            'geometry_clipped_fids_sha256'],
        'repair_count': stats['topology_repair']['count'],
        'repair_sha256': stats['topology_repair']['fids_sha256'],
        'z_count': stats['dimensional_normalization']['source_3d_count'],
        'z_sha256': stats['dimensional_normalization'][
            'source_3d_fids_sha256'],
        'z_coordinate_count': stats['dimensional_normalization'][
            'source_3d_coordinate_count'],
        'z_zero_coordinate_count': stats['dimensional_normalization'][
            'zero_z_coordinate_count'],
        'z_nonzero_coordinate_count': stats['dimensional_normalization'][
            'nonzero_z_coordinate_count'],
        'z_nonzero_fids_sha256': stats['dimensional_normalization'][
            'nonzero_z_fids_sha256'],
        'z_nonzero_records_sha256': stats['dimensional_normalization'][
            'nonzero_z_records_sha256'],
    }
    if observed != contract:
        raise RuntimeError(
            f'{key} geometry/clip contract changed: '
            f'expected={contract}, observed={observed}')


def _stream_source(key, roots, sequence, clip):
    spec = SOURCE_SPECS[key]
    transformer = Transformer.from_crs(
        spec['native_crs'], 'EPSG:4326', always_xy=True)
    source_types, output_types = Counter(), Counter()
    source_ids, empty, unusable, outside, clipped, repairs, source_3d = (
        [], [], [], [], [], [], [])
    z_coordinate_count = 0
    z_zero_coordinate_count = 0
    z_nonzero_records = []
    encoding_fids = {row['fid'] for row in ENCODING_EXCLUSIONS[key]}
    observed_encoding = []
    with fiona.open(roots[spec['archive']], layer=spec['layer']) as rows, \
            open(sequence, 'w', encoding='utf-8') as output:
        for row in rows:
            source_record_id = str(row.id)
            fid = int(row.id) + spec['fid_shift']
            source_ids.append(fid)
            raw_geometry = row['geometry']
            if not raw_geometry:
                empty.append(fid)
                continue
            geometry = shapely_shape(raw_geometry)
            source_types[geometry.geom_type] += 1
            if geometry.is_empty:
                empty.append(fid)
                continue
            if geometry.has_z:
                source_3d.append(fid)
                coordinates = shapely.get_coordinates(
                    geometry, include_z=True)
                z_values = [float(coordinate[2]) for coordinate in coordinates]
                if any(not math.isfinite(value) for value in z_values):
                    raise RuntimeError(
                        f'{key} feature {fid} has nonfinite source Z')
                z_coordinate_count += len(z_values)
                z_zero_coordinate_count += sum(value == 0.0 for value in z_values)
                nonzero = [value for value in z_values if value != 0.0]
                if nonzero:
                    z_nonzero_records.append({
                        'fid': fid, 'source_record_id': source_record_id,
                        'coordinate_count': len(z_values),
                        'nonzero_coordinate_count': len(nonzero),
                        'z_min': min(nonzero), 'z_max': max(nonzero),
                        'source_geometry_sha256': _canonical_sha256(
                            shapely_mapping(geometry)),
                    })
                geometry = shapely_transform(
                    lambda x, y, z=None: (x, y), geometry)
            if spec['kind'] == 'line' and (
                    not geometry.is_valid or geometry.length <= 0):
                unusable.append({
                    'fid': fid, 'source_record_id': source_record_id,
                    'reason': shapely_explain_validity(geometry),
                    'source_type': geometry.geom_type,
                    'native_length': float(geometry.length),
                    'source_geometry_sha256': _canonical_sha256(
                        shapely_mapping(geometry)),
                })
                continue
            if spec['kind'] == 'polygon':
                geometry, repair = _repair_polygon(geometry, fid)
                if repair is not None:
                    repairs.append(repair)
            elif not geometry.is_valid:
                raise RuntimeError(
                    f'{key} feature {fid} is invalid: '
                    f'{shapely_explain_validity(geometry)}')
            geometry = shapely_transform(transformer.transform, geometry)
            changed = not clip['prepared'].covers(geometry)
            if changed:
                geometry = geometry.intersection(clip['boundary'])
            geometry = _same_dimension(geometry, spec['kind'])
            if geometry is None:
                outside.append(fid)
                continue
            if changed:
                clipped.append(fid)
            if fid in encoding_fids:
                observed_encoding.append(fid)
                continue
            output_types[geometry.geom_type] += 1
            feature = {
                'type': 'Feature', 'id': fid,
                'properties': _normalize_properties(
                    key, fid, source_record_id, dict(row['properties'])),
                'geometry': shapely_mapping(geometry),
            }
            _write_feature(output, feature)
    if observed_encoding != sorted(encoding_fids):
        raise RuntimeError(
            f'{key} encoding exclusion inventory changed: '
            f'{observed_encoding} != {sorted(encoding_fids)}')
    tiled_ids = sorted(set(source_ids) - set(empty) -
                       {row['fid'] for row in unusable} - set(outside) -
                       encoding_fids)
    stats = {
        'source_records': len(source_ids), 'n': len(tiled_ids),
        'source_object_ids_sha256': _canonical_sha256(source_ids),
        'tileable_object_ids': tiled_ids,
        'tileable_object_ids_sha256': _canonical_sha256(tiled_ids),
        'source_geometry_types': dict(sorted(source_types.items())),
        'tiled_geometry_types': dict(sorted(output_types.items())),
        'empty_geometry_count': len(empty), 'empty_geometry_fids': empty,
        'empty_geometry_fids_sha256': _canonical_sha256(empty),
        'unusable_source_geometry': {
            'status': ('reviewed_pinned_source_geometry_exclusion'
                       if unusable else
                       'reviewed_no_unusable_source_geometry'),
            'method': 'no_geometry_fabrication_source_record_omitted',
            'count': len(unusable),
            'fids': [row['fid'] for row in unusable],
            'fids_sha256': _canonical_sha256(
                [row['fid'] for row in unusable]),
            'records': unusable,
            'records_sha256': _canonical_sha256(unusable),
        },
        'topology_repair': _repair_evidence(repairs),
        'dimensional_normalization': {
            'status': ('source_3d_z_dimension_audited_and_removed_for_2d_mvt'
                       if source_3d else 'source_2d'),
            'source_3d_count': len(source_3d),
            'source_3d_fids': source_3d,
            'source_3d_fids_sha256': _canonical_sha256(source_3d),
            'source_3d_coordinate_count': z_coordinate_count,
            'zero_z_coordinate_count': z_zero_coordinate_count,
            'nonzero_z_coordinate_count': (
                z_coordinate_count - z_zero_coordinate_count),
            'nonzero_z_fids': [row['fid'] for row in z_nonzero_records],
            'nonzero_z_fids_sha256': _canonical_sha256(
                [row['fid'] for row in z_nonzero_records]),
            'nonzero_z_records': z_nonzero_records,
            'nonzero_z_records_sha256': _canonical_sha256(z_nonzero_records),
            'method': ('inventory every finite source Z; preserve exact '
                       'nonzero-Z evidence; remove Z before CRS transformation '
                       'because MVT geometry is two-dimensional'
                       if source_3d else None),
        },
        'encoding_exclusions': {
            'status': ('reviewed_below_encoding_scale_exclusion'
                       if encoding_fids else
                       'reviewed_no_below_encoding_exclusions'),
            'reason_code': 'below_mvt_maxzoom_encoding_resolution',
            'method': 'no_geometry_fabrication_source_record_omitted',
            'tippecanoe_maxzoom': TIPPECANOE_MAXZOOM,
            'tippecanoe_full_detail': TIPPECANOE_FULL_DETAIL,
            'count': len(encoding_fids),
            'fids': sorted(encoding_fids),
            'fids_sha256': _canonical_sha256(sorted(encoding_fids)),
            'records': ENCODING_EXCLUSIONS[key],
            'records_sha256': _canonical_sha256(ENCODING_EXCLUSIONS[key]),
        },
        'spatial_clip': {
            'ordering': ('native_geometry_validation_and_repair_then_'
                         'epsg4326_transform_then_state_intersection'),
            'fully_outside_count': len(outside),
            'fully_outside_fids': outside,
            'fully_outside_fids_sha256': _canonical_sha256(outside),
            'geometry_clipped_count': len(clipped),
            'geometry_clipped_fids': clipped,
            'geometry_clipped_fids_sha256': _canonical_sha256(clipped),
            'geometry_unchanged_count': (
                len(source_ids) - len(empty) - len(unusable) - len(outside) -
                len(clipped)),
        },
        'sequence_bytes': os.path.getsize(sequence),
        'sequence_sha256': _sha256(sequence),
    }
    _assert_geometry_contract(key, stats)
    pinned = SOURCE_SEQUENCE_SHA256[key]
    if pinned is not None and stats['sequence_sha256'] != pinned:
        raise RuntimeError(
            f'{key} normalized source content changed: '
            f'{stats["sequence_sha256"]} != {pinned}')
    return stats


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


def _run_tippecanoe(output, layers, attribution):
    basename = os.path.basename(output)
    archive_name = ARCHIVE_NAMES.get(basename)
    directory = os.path.realpath(os.path.dirname(output))
    if (archive_name is None or ARCHIVE_ATTRIBUTIONS.get(basename) != attribution
            or any(os.path.realpath(os.path.dirname(sequence)) != directory
                   for _, sequence in layers)):
        raise RuntimeError(f'unregistered Utah archive/input path: {output}')
    command = [
        'tippecanoe', '--force', '--output', basename,
        f'--name={archive_name}', f'--description={archive_name}',
        '--minimum-zoom=0', f'--maximum-zoom={TIPPECANOE_MAXZOOM}',
        f'--full-detail={TIPPECANOE_FULL_DETAIL}', '--drop-rate=1',
        '--no-feature-limit', '--no-tile-size-limit',
        '--no-tiny-polygon-reduction-at-maximum-zoom',
        '--simplify-only-low-zooms', '--quiet',
        f'--attribution={attribution}',
    ]
    for layer, sequence in layers:
        command.extend(('-L', f'{layer}:{os.path.basename(sequence)}'))
    subprocess.run(command, check=True, cwd=directory)


def _pmtiles_json_metadata(path):
    with open(path, 'rb') as source:
        head = source.read(127)
        if len(head) != 127 or head[:7] != b'PMTiles' or head[7] != 3:
            raise RuntimeError(f'{path} has an invalid PMTiles v3 header')
        metadata_offset, metadata_length = struct.unpack_from('<2Q', head, 24)
        compression = head[97]
        source.seek(metadata_offset)
        payload = source.read(metadata_length)
    if compression == 2:
        payload = gzip.decompress(payload)
    elif compression != 1:
        raise RuntimeError(f'{path} has unsupported metadata compression')
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f'{path} has invalid JSON metadata') from exc
    if not isinstance(value, dict):
        raise RuntimeError(f'{path} PMTiles metadata is not an object')
    return value


def _assert_path_independent_metadata(path, key):
    metadata = _pmtiles_json_metadata(path)
    options = metadata.get('generator_options')
    basename = os.path.basename(path)
    attribution = ARCHIVE_ATTRIBUTIONS[basename]
    expected_tokens = [f'--output {basename}'] + [
        f'-L {layer}:{_source_by_layer()[layer]}.geojsonseq'
        for layer in _artifact_layers()[key]]
    if (metadata.get('name') != key or metadata.get('description') != key or
            metadata.get('attribution') != attribution or
            metadata.get('generator') != f'tippecanoe {TIPPECANOE_VERSION}' or
            not isinstance(options, str) or '\\' in options or
            any(token not in options for token in expected_tokens)):
        raise RuntimeError(f'{key} PMTiles metadata is not path-independent')
    serialized = json.dumps(metadata, sort_keys=True, separators=(',', ':'))
    forbidden = (PRIVATE_STAGING_ROOT, os.path.dirname(path), 'tile-set-a',
                 'tile-set-b', 'nwmm-ut-baselines-')
    leaks = [value for value in forbidden if value and value in serialized]
    if leaks:
        raise RuntimeError(f'{key} PMTiles metadata leaks private paths: {leaks}')
    return {
        'status': 'complete_path_free_reproducible_metadata',
        'name': metadata['name'],
        'metadata_sha256': _canonical_sha256(metadata),
        'generator_options_sha256': _canonical_sha256(options),
    }


def _artifact_layers():
    return {
        'ut_ugs_map179dm_500k': (
            'ut_ugs_map179dm_geology', 'ut_ugs_map179dm_structures'),
        'ut_ugs_ds7_quaternary_faults': (
            'ut_ugs_ds7_quaternary_faults',),
        'ut_ugs_ofr695_mining_districts': (
            'ut_ugs_ofr695_mining_districts',),
        'ut_ugs_ofr757_umos': ('ut_ugs_ofr757_umos',),
    }


def _source_by_layer():
    return {spec['layer_id']: key for key, spec in SOURCE_SPECS.items()}


def _validate_pmtiles(path, layers, *, pmtiles_header=None):
    if pmtiles_header is None:
        pmtiles_header = _strict_pmtiles_header
    metadata = pmtiles_header(
        path, list(layers),
        {layer: LAYER_REQUIREMENTS[layer] for layer in layers},
        verify_feature_properties=True, expected_state='UT',
        expected_bounds=[UT_BOUNDS], collect_feature_ids=True)
    if set(metadata['source_layers']) != set(layers):
        raise RuntimeError(f'{path} contains unexpected source layers')
    if metadata['minzoom'] != 0 or metadata['maxzoom'] != TIPPECANOE_MAXZOOM:
        raise RuntimeError(f'{path} zoom contract changed')
    bounds = metadata['bounds']
    tolerance = 2e-6
    if (bounds[0] < UT_BOUNDS[0] - tolerance or
            bounds[1] < UT_BOUNDS[1] - tolerance or
            bounds[2] > UT_BOUNDS[2] + tolerance or
            bounds[3] > UT_BOUNDS[3] + tolerance):
        raise RuntimeError(f'{path} bounds escape Utah: {bounds}')
    if any(metadata['semantic_layer_counts'].get(layer, 0) <= 0
           for layer in layers):
        raise RuntimeError(f'{path} has an empty declared source layer')
    key = ARCHIVE_NAMES.get(os.path.basename(path))
    if key is None:
        raise RuntimeError(f'{path} has no Utah archive identity')
    metadata['reproducible_metadata'] = _assert_path_independent_metadata(
        path, key)
    return metadata


def _source_id_inventory(layer, stats, metadata):
    expected = stats['tileable_object_ids']
    observed = metadata.get('maxzoom_feature_ids', {}).get(layer) or []
    if observed != sorted(set(observed)) or observed != expected:
        raise RuntimeError(
            f'{layer} maxzoom IDs do not reconcile: '
            f'expected={len(expected)}, observed={len(observed)}, '
            f'missing={sorted(set(expected) - set(observed))[:100]}, '
            f'extra={sorted(set(observed) - set(expected))[:100]}')
    return {
        'status': 'complete',
        'source_records': stats['source_records'],
        'tileable_source_records': len(expected),
        'unique_maxzoom_ids': len(observed),
        'source_object_ids_sha256': stats['source_object_ids_sha256'],
        'tileable_source_object_ids_sha256': _canonical_sha256(expected),
        'maxzoom_object_ids_sha256': _canonical_sha256(observed),
        'maxzoom_feature_instances': metadata.get(
            'maxzoom_feature_instances', {}).get(layer),
    }


def _artifact_fields(path, metadata):
    return {
        'bytes': os.path.getsize(path), 'sha256': _sha256(path),
        'bounds': metadata['bounds'], 'minzoom': metadata['minzoom'],
        'maxzoom': metadata['maxzoom'],
        'field_types': metadata['field_types'],
        'semantic_tile_feature_counts': metadata['semantic_layer_counts'],
        'reproducible_metadata': metadata['reproducible_metadata'],
    }


def _browser_descriptor(key, file, metadata, stats):
    source_lookup = _source_by_layer()
    layers = []
    for layer in _artifact_layers()[key]:
        contract = BROWSER_LAYER_CONTRACTS[layer]
        layers.append({
            'layer_id': f'{layer}_baseline', 'title': contract['title'],
            'source_layer': layer, 'geometry': contract['geometry'],
            'style': json.loads(json.dumps(contract['style'])),
            'required_properties': list(LAYER_REQUIREMENTS[layer]),
            'feature_count': stats[source_lookup[layer]]['n'],
            'bounds': list(metadata['bounds']),
            'activation_zoom': contract['activation_zoom'],
            'default_visible': False,
            'state_filter': _state_filter(),
            'semantic_note': contract['semantic_note'],
        })
    return {
        'schema_version': 1,
        'status': 'proposed_lazy_state_survey_descriptor',
        'manifest_key': key, 'file': file,
        'protocol_url': f'pmtiles://{file}', 'state': 'UT', 'lazy': True,
        'default_visible': False,
        'activation_zoom': min(row['activation_zoom'] for row in layers),
        'bounds': list(metadata['bounds']), 'minzoom': metadata['minzoom'],
        'maxzoom': metadata['maxzoom'], 'state_filter': _state_filter(),
        'layers': layers,
    }


def _stats_manifest(stats, clip_manifest):
    source = {key: value for key, value in stats.items()
              if key not in {'spatial_clip', 'tileable_object_ids'}}
    spatial = dict(clip_manifest)
    spatial.update(stats['spatial_clip'])
    return source, spatial


def _archive_source_manifest(key, downloads, catalogs):
    contract = ARCHIVE_CONTRACTS[key]
    return {
        'authority': 'Utah Geological Survey', **catalogs[key],
        'bulk_bytes': downloads[key]['bytes'],
        'bulk_sha256': downloads[key]['sha256'],
        'archive_member_count': downloads[key]['member_count'],
        'archive_member_inventory_sha256':
            downloads[key]['member_inventory_sha256'],
    }


def _build_entries(paths, metadata, stats, clip_manifest, downloads,
                   catalogs, source_layers):
    entries = {}
    source_lookup = _source_by_layer()
    for key, path in paths.items():
        layers = _artifact_layers()[key]
        archive_key = {
            'ut_ugs_map179dm_500k': 'map179',
            'ut_ugs_ds7_quaternary_faults': 'ds7',
            'ut_ugs_ofr695_mining_districts': 'districts',
            'ut_ugs_ofr757_umos': 'umos',
        }[key]
        file = os.path.relpath(BASELINE_KEYS[key], SITE)
        inventories, per_layer = {}, {}
        for layer in layers:
            source_key = source_lookup[layer]
            source_manifest, spatial = _stats_manifest(
                stats[source_key], clip_manifest)
            source_manifest['typed_source_layer'] = source_layers[source_key]
            inventory = _source_id_inventory(
                layer, stats[source_key], metadata[key])
            source_manifest['source_id_inventory'] = inventory
            inventories[layer] = inventory
            per_layer[layer] = {
                'source_inventory': source_manifest,
                'spatial_clip': spatial,
                'source_id_inventory': inventory,
            }
        entry = {
            'schema_version': 1, 'status': 'baseline_not_release',
            'state': 'UT', 'format': 'pmtiles', 'file': file,
            'source': _archive_source_manifest(
                archive_key, downloads, catalogs),
            'retrieved': TODAY, 'n': sum(stats[source_lookup[layer]]['n']
                                        for layer in layers),
            'states': {'UT': sum(stats[source_lookup[layer]]['n']
                                 for layer in layers)},
            'by_layer': per_layer,
            'source_id_inventory': inventories,
            'required_properties': {
                layer: list(LAYER_REQUIREMENTS[layer]) for layer in layers},
            'atomic_group': {
                'id': ATOMIC_GROUP_ID, 'status': 'baseline_not_release',
                'keys': sorted(BASELINE_KEYS),
            },
            'provenance_note': ACCEPTED_PROVENANCE_NOTE,
            **_artifact_fields(path, metadata[key]),
        }
        if len(layers) == 1:
            entry['source_layer'] = layers[0]
        else:
            entry['source_layers'] = list(layers)
        if key == 'ut_ugs_ds7_quaternary_faults':
            entry['source']['selected_layer'] = 'UQFD25_DS7_full'
            entry['source']['excluded_companion_layers'] = (
                ACCEPTED_DS7_COMPANION_EXCLUSIONS)
        if key == 'ut_ugs_ofr757_umos':
            entry['excluded_source_properties'] = (
                ACCEPTED_UMOS_PROPERTY_EXCLUSIONS)
        entry['browser_descriptor'] = _browser_descriptor(
            key, file, metadata[key], stats)
        entries[key] = entry
    return entries


def _tile_set(directory, sequences):
    os.makedirs(directory, exist_ok=False)
    local = {}
    for key, source in sequences.items():
        target = os.path.join(directory, f'{key}.geojsonseq')
        shutil.copyfile(source, target)
        if (os.path.getsize(target) != os.path.getsize(source) or
                _sha256(target) != _sha256(source)):
            raise RuntimeError(f'{key} private tile input copy changed')
        local[key] = target
    paths = {
        key: os.path.join(directory, os.path.basename(path))
        for key, path in BASELINE_KEYS.items()}
    _run_tippecanoe(paths['ut_ugs_map179dm_500k'], (
        ('ut_ugs_map179dm_geology', local['geology_units']),
        ('ut_ugs_map179dm_structures', local['geology_lines'])),
        ARCHIVE_ATTRIBUTIONS['ugs-map179dm-500k.pmtiles'])
    _run_tippecanoe(paths['ut_ugs_ds7_quaternary_faults'], (
        ('ut_ugs_ds7_quaternary_faults', local['faults']),),
        ARCHIVE_ATTRIBUTIONS['ugs-ds7-quaternary-faults.pmtiles'])
    _run_tippecanoe(paths['ut_ugs_ofr695_mining_districts'], (
        ('ut_ugs_ofr695_mining_districts', local['districts']),),
        ARCHIVE_ATTRIBUTIONS['ugs-ofr695-mining-districts.pmtiles'])
    _run_tippecanoe(paths['ut_ugs_ofr757_umos'], (
        ('ut_ugs_ofr757_umos', local['umos']),),
        ARCHIVE_ATTRIBUTIONS['ugs-ofr757-umos.pmtiles'])
    return paths


def _validate_set(paths, *, pmtiles_header=None):
    return {key: _validate_pmtiles(
        path, _artifact_layers()[key], pmtiles_header=pmtiles_header)
            for key, path in paths.items()}


def _strict_manifest_bytes():
    with open(MANIFEST, 'rb') as source:
        payload = source.read()
    try:
        value = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f'manifest is invalid JSON: {exc}') from exc
    if not isinstance(value, dict) or not isinstance(
            value.get('national_baselines'), dict):
        raise RuntimeError('manifest national_baselines is missing')
    return payload, value


def _manifest_without_utah(manifest):
    value = json.loads(json.dumps(manifest))
    baselines = value.get('national_baselines') or {}
    for key in BASELINE_KEYS:
        baselines.pop(key, None)
    return value


def _unrelated_manifest_sha256(manifest):
    return _canonical_sha256(_manifest_without_utah(manifest))


def _publish(pending, entries):
    if set(pending) != set(BASELINE_KEYS) or set(entries) != set(BASELINE_KEYS):
        raise RuntimeError('Utah publication requires the exact atomic set')
    pending_metadata = _validate_set(pending)
    for key in BASELINE_KEYS:
        _validate_reviewed_entry(
            key, entries[key], pending[key], pending_metadata[key])
    os.makedirs(OUT_DIR, exist_ok=True)
    manifest_bytes, manifest = _strict_manifest_bytes()
    before_unrelated = _unrelated_manifest_sha256(manifest)
    baselines = manifest['national_baselines']
    baselines.update(json.loads(json.dumps(entries)))
    if _unrelated_manifest_sha256(manifest) != before_unrelated:
        raise RuntimeError('Utah publication changed unrelated manifest data')
    payload = (json.dumps(manifest, indent=2, sort_keys=True) + '\n').encode()
    backups, pending_installs = {}, {}
    manifest_backup = manifest_bytes
    try:
        for key, final_path in BASELINE_KEYS.items():
            if os.path.exists(final_path):
                backup = final_path + '.ut-backup'
                if os.path.exists(backup):
                    raise RuntimeError(f'stale Utah backup exists: {backup}')
                os.replace(final_path, backup)
                backups[final_path] = backup
            install = final_path + '.ut-pending'
            shutil.copyfile(pending[key], install)
            os.chmod(install, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
            if (_sha256(install) != entries[key]['sha256'] or
                    os.path.getsize(install) != entries[key]['bytes']):
                raise RuntimeError(f'{key} pending installation changed')
            pending_installs[final_path] = install
        manifest_pending = MANIFEST + '.ut-pending'
        with open(manifest_pending, 'wb') as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        for final_path, install in pending_installs.items():
            os.replace(install, final_path)
        os.replace(manifest_pending, MANIFEST)
        _, installed = _strict_manifest_bytes()
        if _unrelated_manifest_sha256(installed) != before_unrelated:
            raise RuntimeError('post-publication unrelated manifest changed')
        for key, final_path in BASELINE_KEYS.items():
            if (_sha256(final_path) != entries[key]['sha256'] or
                    os.path.getsize(final_path) != entries[key]['bytes']):
                raise RuntimeError(f'{key} installed artifact mismatch')
        for backup in backups.values():
            os.unlink(backup)
        return {
            'status': 'atomic_four_archive_install',
            'keys': sorted(BASELINE_KEYS),
            'unrelated_manifest_sha256': before_unrelated,
        }
    except BaseException:
        for install in pending_installs.values():
            if os.path.exists(install):
                os.unlink(install)
        manifest_pending = MANIFEST + '.ut-pending'
        if os.path.exists(manifest_pending):
            os.unlink(manifest_pending)
        with open(MANIFEST + '.ut-rollback', 'wb') as output:
            output.write(manifest_backup)
            output.flush()
            os.fsync(output.fileno())
        os.replace(MANIFEST + '.ut-rollback', MANIFEST)
        for final_path in reversed(list(BASELINE_KEYS.values())):
            backup = backups.get(final_path)
            if backup and os.path.exists(backup):
                if os.path.exists(final_path):
                    os.unlink(final_path)
                os.replace(backup, final_path)
            elif not backup and os.path.exists(final_path):
                os.unlink(final_path)
        raise


def _preflight():
    if not shutil.which('tippecanoe'):
        raise RuntimeError('tippecanoe is required')
    if _tippecanoe_version() != TIPPECANOE_VERSION:
        raise RuntimeError(f'tippecanoe must remain {TIPPECANOE_VERSION}')
    if fiona is None or Transformer is None:
        raise RuntimeError(
            'Fiona and pyproj are required; use '
            '/Users/matthewlew/miniconda3/bin/python')
    if any(value is None for value in (
            shapely, shapely_make_valid, shapely_mapping, shapely_shape,
            shapely_transform, shapely_unary_union, shapely_prepare,
            shapely_explain_validity)):
        raise RuntimeError('Shapely 2.x is required')
    if (shapely.__version__ != '2.0.3' or
            shapely.geos_version_string != '3.11.3'):
        raise RuntimeError(
            'Utah geometry audit requires Shapely 2.0.3 / GEOS 3.11.3')


def build(*, publish=False, grace_seconds=0, double_build=True):
    """Build privately by default; publish only with explicit authorization."""
    _preflight()
    if not 0 <= grace_seconds <= 60:
        raise RuntimeError('publication grace must be from 0 to 60 seconds')
    if publish and (not double_build or grace_seconds < 30):
        raise RuntimeError(
            'Utah publication requires a double build and >=30-second grace')
    staging = _ensure_private_staging_root()
    with tempfile.TemporaryDirectory(
            prefix='nwmm-ut-baselines-', dir=staging) as temp:
        catalogs = _verify_authority_catalogs()
        roots, downloads = _prepare_sources(temp)
        source_layers = _verify_source_layers(roots)
        clip = _load_ut_clip()

        passes, pass_stats = [], []
        for pass_number in (1, 2):
            sequences = {key: os.path.join(
                temp, f'pass-{pass_number}-{key}.geojsonseq')
                for key in SOURCE_SPECS}
            stats = {key: _stream_source(
                key, roots, sequences[key], clip) for key in SOURCE_SPECS}
            passes.append(sequences)
            pass_stats.append(stats)
        for key in SOURCE_SPECS:
            if (os.path.getsize(passes[0][key]) !=
                    os.path.getsize(passes[1][key]) or
                    _sha256(passes[0][key]) != _sha256(passes[1][key]) or
                    pass_stats[0][key] != pass_stats[1][key]):
                raise RuntimeError(f'{key} changed across full source passes')

        if _verify_source_layers(roots) != source_layers:
            raise RuntimeError('Utah typed source layers changed during build')
        if _verify_authority_catalogs() != catalogs:
            raise RuntimeError('Utah official catalog identity changed during build')
        for key, contract in ARCHIVE_CONTRACTS.items():
            path = os.path.join(temp, contract['filename'])
            if (_sha256(path) != contract['sha256'] or
                    os.path.getsize(path) != contract['bytes']):
                raise RuntimeError(f'{key} source archive changed during build')

        first_paths = _tile_set(os.path.join(temp, 'tile-set-a'), passes[0])
        first_metadata = _validate_set(first_paths)
        entries = _build_entries(
            first_paths, first_metadata, pass_stats[0], clip['manifest'],
            downloads, catalogs, source_layers)
        deterministic = {}
        if double_build:
            second_paths = _tile_set(
                os.path.join(temp, 'tile-set-b'), passes[0])
            second_metadata = _validate_set(second_paths)
            _build_entries(
                second_paths, second_metadata, pass_stats[0],
                clip['manifest'], downloads, catalogs, source_layers)
            for key in BASELINE_KEYS:
                first = (os.path.getsize(first_paths[key]),
                         _sha256(first_paths[key]))
                second = (os.path.getsize(second_paths[key]),
                          _sha256(second_paths[key]))
                if first != second:
                    raise RuntimeError(
                        f'{key} PMTiles is path-dependent: {first} != {second}')
                deterministic[key] = {
                    'status': 'two_byte_identical_builds',
                    'bytes': first[0], 'sha256': first[1],
                }
                entries[key]['deterministic_rebuild'] = deterministic[key]

            # A deterministic replacement can still be a consistently
            # truncated dataset.  Bind the candidate to the independently
            # reviewed official-source and exact artifact generation before
            # it can approach the publication transaction.
            for key in BASELINE_KEYS:
                _validate_reviewed_entry(
                    key, entries[key], first_paths[key], first_metadata[key])

        report = {
            'status': ('published_baseline_not_release' if publish else
                       'private_baseline_not_release'),
            'state': 'UT', 'release_changed': False,
            'atomic_group': {'id': ATOMIC_GROUP_ID,
                             'keys': sorted(BASELINE_KEYS)},
            'catalog_identity': catalogs,
            'source_downloads': downloads,
            'typed_source_layers': source_layers,
            'source_sequences': {key: {
                'source_records': pass_stats[0][key]['source_records'],
                'tiled_records': pass_stats[0][key]['n'],
                'bytes': pass_stats[0][key]['sequence_bytes'],
                'sha256': pass_stats[0][key]['sequence_sha256'],
                'empty_geometry_count':
                    pass_stats[0][key]['empty_geometry_count'],
                'unusable_source_geometry_count':
                    pass_stats[0][key]['unusable_source_geometry']['count'],
                'fully_outside_count':
                    pass_stats[0][key]['spatial_clip']['fully_outside_count'],
                'geometry_clipped_count':
                    pass_stats[0][key]['spatial_clip']['geometry_clipped_count'],
                'topology_repair_count':
                    pass_stats[0][key]['topology_repair']['count'],
                'encoding_exclusion_count':
                    pass_stats[0][key]['encoding_exclusions']['count'],
            } for key in SOURCE_SPECS},
            'artifacts': {key: {
                'features': entries[key]['n'],
                'bytes': entries[key]['bytes'],
                'sha256': entries[key]['sha256'],
                'bounds': entries[key]['bounds'],
                'minzoom': entries[key]['minzoom'],
                'maxzoom': entries[key]['maxzoom'],
                'source_layers': list(_artifact_layers()[key]),
                'field_types': entries[key]['field_types'],
                'semantic_tile_feature_counts':
                    entries[key]['semantic_tile_feature_counts'],
                'maxzoom_unique_feature_ids': {
                    layer: len(ids) for layer, ids in
                    first_metadata[key]['maxzoom_feature_ids'].items()},
                'maxzoom_feature_instances':
                    first_metadata[key]['maxzoom_feature_instances'],
            } for key in BASELINE_KEYS},
            'browser_descriptors': {key: entries[key]['browser_descriptor']
                                    for key in BASELINE_KEYS},
            'deterministic_rebuild': deterministic,
        }
        if publish:
            if any(value is None for value in SOURCE_SEQUENCE_SHA256.values()):
                raise RuntimeError(
                    'Utah source-sequence fingerprints are not publication-pinned')
            print('Utah private archives validated; atomic publication begins '
                  f'in {grace_seconds} seconds')
            time.sleep(grace_seconds)
            report['publication_audit'] = _publish(first_paths, entries)
        print(json.dumps(report, indent=2, sort_keys=True))
        return report


def _validate_browser_descriptor(key, entry):
    descriptor = entry.get('browser_descriptor')
    layers = _artifact_layers()[key]
    if (not isinstance(descriptor, dict) or descriptor.get('schema_version') != 1
            or descriptor.get('status') !=
            'proposed_lazy_state_survey_descriptor' or
            descriptor.get('manifest_key') != key or
            descriptor.get('file') != entry.get('file') or
            descriptor.get('protocol_url') !=
            f"pmtiles://{entry.get('file')}" or
            descriptor.get('state') != 'UT' or descriptor.get('lazy') is not True
            or descriptor.get('default_visible') is not False or
            descriptor.get('bounds') != entry.get('bounds') or
            descriptor.get('minzoom') != entry.get('minzoom') or
            descriptor.get('maxzoom') != entry.get('maxzoom') or
            descriptor.get('state_filter') != _state_filter() or
            not isinstance(descriptor.get('layers'), list) or
            len(descriptor['layers']) != len(layers)):
        raise RuntimeError(f'{key} browser descriptor is invalid')
    by_source = {row.get('source_layer'): row for row in descriptor['layers']
                 if isinstance(row, dict)}
    if set(by_source) != set(layers):
        raise RuntimeError(f'{key} browser descriptor layers are not exact')
    for layer in layers:
        row = by_source[layer]
        contract = BROWSER_LAYER_CONTRACTS[layer]
        if (row.get('default_visible') is not False or
                row.get('state_filter') != _state_filter() or
                row.get('style') != contract['style'] or
                row.get('activation_zoom') != contract['activation_zoom'] or
                row.get('required_properties') != LAYER_REQUIREMENTS[layer] or
                row.get('bounds') != entry.get('bounds')):
            raise RuntimeError(f'{key}/{layer} browser descriptor changed')


def _accepted_source_contract(key):
    archive_key = _ARCHIVE_BY_BASELINE[key]
    archive = ARCHIVE_CONTRACTS[archive_key]
    result = {
        'authority': 'Utah Geological Survey',
        **ACCEPTED_CATALOG_CONTRACTS[archive_key],
        'bulk_bytes': archive['bytes'], 'bulk_sha256': archive['sha256'],
        'archive_member_count': archive['member_count'],
        'archive_member_inventory_sha256':
            archive['member_inventory_sha256'],
    }
    if key == 'ut_ugs_ds7_quaternary_faults':
        result['selected_layer'] = 'UQFD25_DS7_full'
        result['excluded_companion_layers'] = (
            ACCEPTED_DS7_COMPANION_EXCLUSIONS)
    return result


def _accepted_encoding_evidence(source_key):
    records = ENCODING_EXCLUSIONS[source_key]
    return {
        'status': ('reviewed_below_encoding_scale_exclusion' if records else
                   'reviewed_no_below_encoding_exclusions'),
        'reason_code': 'below_mvt_maxzoom_encoding_resolution',
        'method': 'no_geometry_fabrication_source_record_omitted',
        'tippecanoe_maxzoom': TIPPECANOE_MAXZOOM,
        'tippecanoe_full_detail': TIPPECANOE_FULL_DETAIL,
        'count': len(records), 'fids': sorted(row['fid'] for row in records),
        'fids_sha256': _canonical_sha256(
            sorted(row['fid'] for row in records)),
        'records': records, 'records_sha256': _canonical_sha256(records),
    }


def _require_hashed_list(value, *, count, expected_sha256, label):
    if (not isinstance(value, list) or len(value) != count or
            _canonical_sha256(value) != expected_sha256):
        raise RuntimeError(f'{label} exact list inventory is invalid')


def _validate_source_evidence(key, layer, row, inventory, metadata):
    """Bind one manifest layer to the reviewed official-source generation."""
    if not isinstance(row, dict) or set(row) != {
            'source_inventory', 'spatial_clip', 'source_id_inventory'}:
        raise RuntimeError(f'{key}/{layer} source evidence schema is invalid')
    source_key = _source_by_layer()[layer]
    spec = SOURCE_SPECS[source_key]
    geometry = GEOMETRY_CONTRACTS[source_key]
    accepted = ACCEPTED_ARTIFACT_CONTRACTS[key]
    expected_n = accepted['maxzoom_unique_feature_ids'][layer]
    ids = metadata['maxzoom_feature_ids'][layer]
    ids_sha256 = _canonical_sha256(ids)

    if row.get('source_id_inventory') != inventory:
        raise RuntimeError(f'{key}/{layer} duplicated source-ID evidence differs')
    source = row.get('source_inventory')
    if not isinstance(source, dict) or source.get('source_id_inventory') != inventory:
        raise RuntimeError(f'{key}/{layer} source inventory is missing')
    expected_source_fields = {
        'source_records': geometry['source_records'], 'n': expected_n,
        'source_object_ids_sha256': geometry['source_object_ids_sha256'],
        'tileable_object_ids_sha256': ids_sha256,
        'source_geometry_types': geometry['source_types'],
        'tiled_geometry_types': geometry['output_types'],
        'empty_geometry_count': geometry['empty_count'],
        'empty_geometry_fids_sha256': geometry['empty_sha256'],
        'sequence_bytes': SOURCE_SEQUENCE_BYTES[source_key],
        'sequence_sha256': SOURCE_SEQUENCE_SHA256[source_key],
    }
    if any(source.get(field) != value
           for field, value in expected_source_fields.items()):
        raise RuntimeError(
            f'{key}/{layer} reviewed source/sequence contract changed')
    _require_hashed_list(
        source.get('empty_geometry_fids'), count=geometry['empty_count'],
        expected_sha256=geometry['empty_sha256'],
        label=f'{key}/{layer} empty geometry')

    typed = source.get('typed_source_layer')
    typed_projection_fields = ('driver', 'crs', 'schema', 'bounds', 'n')
    if (not isinstance(typed, dict) or typed.get('n') !=
            geometry['source_records'] or typed.get('crs') != spec['native_crs'] or
            typed.get('manifest_sha256') != spec['manifest_sha256'] or
            typed.get('source_fids_sha256') != spec['source_fids_sha256'] or
            not all(isinstance(typed.get(field), int) and
                    not isinstance(typed.get(field), bool)
                    for field in ('minimum_source_fid', 'maximum_source_fid')) or
            typed['minimum_source_fid'] > typed['maximum_source_fid'] or
            _canonical_sha256({field: typed.get(field)
                               for field in typed_projection_fields}) !=
            spec['manifest_sha256']):
        raise RuntimeError(f'{key}/{layer} typed official source changed')

    unusable = source.get('unusable_source_geometry')
    if (not isinstance(unusable, dict) or
            unusable.get('count') != geometry['unusable_count'] or
            unusable.get('fids_sha256') != geometry['unusable_sha256']):
        raise RuntimeError(f'{key}/{layer} unusable-geometry evidence changed')
    _require_hashed_list(
        unusable.get('fids'), count=geometry['unusable_count'],
        expected_sha256=geometry['unusable_sha256'],
        label=f'{key}/{layer} unusable geometry')
    unusable_records = unusable.get('records')
    if (not isinstance(unusable_records, list) or
            len(unusable_records) != geometry['unusable_count'] or
            unusable.get('records_sha256') !=
            _canonical_sha256(unusable_records) or
            [record.get('fid') for record in unusable_records
             if isinstance(record, dict)] != unusable.get('fids')):
        raise RuntimeError(f'{key}/{layer} unusable records are truncated')

    repair = source.get('topology_repair')
    if (not isinstance(repair, dict) or
            repair.get('count') != geometry['repair_count'] or
            repair.get('fids_sha256') != geometry['repair_sha256']):
        raise RuntimeError(f'{key}/{layer} topology-repair evidence changed')
    _require_hashed_list(
        repair.get('fids'), count=geometry['repair_count'],
        expected_sha256=geometry['repair_sha256'],
        label=f'{key}/{layer} topology repair')
    repair_records = repair.get('records')
    if (not isinstance(repair_records, list) or
            len(repair_records) != geometry['repair_count'] or
            repair.get('records_sha256') != _canonical_sha256(repair_records)):
        raise RuntimeError(f'{key}/{layer} repair records are truncated')

    dimensional = source.get('dimensional_normalization')
    expected_dimensional = {
        'source_3d_count': geometry['z_count'],
        'source_3d_fids_sha256': geometry['z_sha256'],
        'source_3d_coordinate_count': geometry['z_coordinate_count'],
        'zero_z_coordinate_count': geometry['z_zero_coordinate_count'],
        'nonzero_z_coordinate_count': geometry['z_nonzero_coordinate_count'],
        'nonzero_z_fids_sha256': geometry['z_nonzero_fids_sha256'],
        'nonzero_z_records_sha256': geometry['z_nonzero_records_sha256'],
    }
    if (not isinstance(dimensional, dict) or
            any(dimensional.get(field) != value
                for field, value in expected_dimensional.items())):
        raise RuntimeError(f'{key}/{layer} dimensional evidence changed')
    _require_hashed_list(
        dimensional.get('source_3d_fids'), count=geometry['z_count'],
        expected_sha256=geometry['z_sha256'],
        label=f'{key}/{layer} source Z')
    nonzero_fids = dimensional.get('nonzero_z_fids')
    _require_hashed_list(
        nonzero_fids, count=len(nonzero_fids or []),
        expected_sha256=geometry['z_nonzero_fids_sha256'],
        label=f'{key}/{layer} nonzero Z')
    nonzero_records = dimensional.get('nonzero_z_records')
    if (not isinstance(nonzero_records, list) or
            dimensional.get('nonzero_z_records_sha256') !=
            _canonical_sha256(nonzero_records) or
            [record.get('fid') for record in nonzero_records
             if isinstance(record, dict)] != nonzero_fids):
        raise RuntimeError(f'{key}/{layer} nonzero-Z records are truncated')

    if source.get('encoding_exclusions') != _accepted_encoding_evidence(source_key):
        raise RuntimeError(f'{key}/{layer} encoding-exclusion evidence changed')

    spatial = row.get('spatial_clip')
    expected_spatial = {
        **ACCEPTED_CLIP_CONTRACT,
        'ordering': ('native_geometry_validation_and_repair_then_'
                     'epsg4326_transform_then_state_intersection'),
        'fully_outside_count': geometry['outside_count'],
        'fully_outside_fids_sha256': geometry['outside_sha256'],
        'geometry_clipped_count': geometry['clipped_count'],
        'geometry_clipped_fids_sha256': geometry['clipped_sha256'],
        'geometry_unchanged_count': (
            geometry['source_records'] - geometry['empty_count'] -
            geometry['unusable_count'] - geometry['outside_count'] -
            geometry['clipped_count']),
    }
    if (not isinstance(spatial, dict) or
            any(spatial.get(field) != value
                for field, value in expected_spatial.items())):
        raise RuntimeError(f'{key}/{layer} authoritative clip evidence changed')
    _require_hashed_list(
        spatial.get('fully_outside_fids'), count=geometry['outside_count'],
        expected_sha256=geometry['outside_sha256'],
        label=f'{key}/{layer} outside clip')
    _require_hashed_list(
        spatial.get('geometry_clipped_fids'), count=geometry['clipped_count'],
        expected_sha256=geometry['clipped_sha256'],
        label=f'{key}/{layer} clipped geometry')

    expected_inventory = {
        'status': 'complete', 'source_records': geometry['source_records'],
        'tileable_source_records': expected_n,
        'unique_maxzoom_ids': expected_n,
        'source_object_ids_sha256': geometry['source_object_ids_sha256'],
        'tileable_source_object_ids_sha256': ids_sha256,
        'maxzoom_object_ids_sha256': ids_sha256,
        'maxzoom_feature_instances':
            metadata['maxzoom_feature_instances'][layer],
    }
    if inventory != expected_inventory:
        raise RuntimeError(f'{key}/{layer} exact source-ID inventory changed')


def _validate_reviewed_entry(key, entry, path, metadata):
    """Reject a self-consistent replacement for the reviewed UT generation."""
    accepted = ACCEPTED_ARTIFACT_CONTRACTS[key]
    layers = _artifact_layers()[key]
    expected_file = os.path.relpath(BASELINE_KEYS[key], SITE)
    declared = entry.get('source_layers') or [entry.get('source_layer')]
    expected_artifact = {
        'n': accepted['n'], 'states': {'UT': accepted['n']},
        'bytes': accepted['bytes'], 'sha256': accepted['sha256'],
        'bounds': accepted['bounds'], 'minzoom': 0,
        'maxzoom': TIPPECANOE_MAXZOOM,
        'semantic_tile_feature_counts':
            accepted['semantic_tile_feature_counts'],
    }
    if (not isinstance(entry, dict) or entry.get('schema_version') != 1 or
            entry.get('status') != 'baseline_not_release' or
            entry.get('state') != 'UT' or entry.get('format') != 'pmtiles' or
            entry.get('file') != expected_file or declared != list(layers) or
            entry.get('retrieved') != ACCEPTED_RETRIEVED or
            entry.get('provenance_note') != ACCEPTED_PROVENANCE_NOTE or
            entry.get('source') != _accepted_source_contract(key) or
            entry.get('atomic_group') != {
                'id': ATOMIC_GROUP_ID, 'status': 'baseline_not_release',
                'keys': sorted(BASELINE_KEYS)} or
            entry.get('required_properties') != {
                layer: LAYER_REQUIREMENTS[layer] for layer in layers} or
            any(entry.get(field) != value
                for field, value in expected_artifact.items())):
        raise RuntimeError(f'{key} reviewed manifest generation is invalid')
    if key == 'ut_ugs_ofr757_umos':
        if entry.get('excluded_source_properties') != (
                ACCEPTED_UMOS_PROPERTY_EXCLUSIONS):
            raise RuntimeError(f'{key} source-property exclusion changed')
    elif 'excluded_source_properties' in entry:
        raise RuntimeError(f'{key} has an unexpected source-property exclusion')
    if (entry.get('deterministic_rebuild') != {
            'status': 'two_byte_identical_builds',
            'bytes': accepted['bytes'], 'sha256': accepted['sha256']}):
        raise RuntimeError(f'{key} deterministic rebuild evidence is missing')
    if (os.path.getsize(path) != accepted['bytes'] or
            _sha256(path) != accepted['sha256']):
        raise RuntimeError(f'{key} is not the reviewed PMTiles generation')
    if (metadata.get('bounds') != accepted['bounds'] or
            metadata.get('minzoom') != 0 or
            metadata.get('maxzoom') != TIPPECANOE_MAXZOOM or
            metadata.get('semantic_layer_counts') !=
            accepted['semantic_tile_feature_counts'] or
            {layer: len(metadata.get('maxzoom_feature_ids', {}).get(layer, []))
             for layer in layers} != accepted['maxzoom_unique_feature_ids']):
        raise RuntimeError(f'{key} decoded reviewed artifact contract changed')
    if (entry.get('field_types') != metadata.get('field_types') or
            entry.get('reproducible_metadata') !=
            metadata.get('reproducible_metadata')):
        raise RuntimeError(f'{key} PMTiles metadata evidence changed')

    inventories = entry.get('source_id_inventory')
    by_layer = entry.get('by_layer')
    if (not isinstance(inventories, dict) or set(inventories) != set(layers) or
            not isinstance(by_layer, dict) or set(by_layer) != set(layers)):
        raise RuntimeError(f'{key} layer evidence is incomplete')
    for layer in layers:
        _validate_source_evidence(
            key, layer, by_layer[layer], inventories[layer], metadata)

    browser_stats = {}
    source_lookup = _source_by_layer()
    for baseline_key, artifact in ACCEPTED_ARTIFACT_CONTRACTS.items():
        for source_layer, count in artifact[
                'maxzoom_unique_feature_ids'].items():
            browser_stats[source_lookup[source_layer]] = {'n': count}
    expected_browser = _browser_descriptor(
        key, entry['file'], metadata, browser_stats)
    if entry.get('browser_descriptor') != expected_browser:
        raise RuntimeError(f'{key} browser descriptor changed')


def validate_manifest_baselines(manifest, *, pmtiles_header=None):
    """Validate a published atomic Utah set without network access."""
    baselines = manifest.get('national_baselines') if isinstance(
        manifest, dict) else None
    if not isinstance(baselines, dict):
        raise RuntimeError('manifest national_baselines is missing')
    present = set(BASELINE_KEYS) & set(baselines)
    if present != set(BASELINE_KEYS):
        raise RuntimeError(
            f'Utah state-survey baseline must be atomic: {sorted(present)}')
    result = {}
    for key, expected_path in BASELINE_KEYS.items():
        entry = baselines[key]
        layers = _artifact_layers()[key]
        if not os.path.isfile(expected_path):
            raise RuntimeError(f'{key} PMTiles artifact is missing')
        metadata = _validate_pmtiles(
            expected_path, layers, pmtiles_header=pmtiles_header)
        _validate_reviewed_entry(key, entry, expected_path, metadata)
        result[key] = {
            'features': entry['n'], 'bytes': entry['bytes'],
            'sha256': entry['sha256']}
    return result


def check():
    with open(MANIFEST, encoding='utf-8') as source:
        manifest = json.load(source)
    validate_manifest_baselines(manifest)
    print('Utah state-survey PMTiles manifest validation passed')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--publish', action='store_true',
                        help='atomically install baseline_not_release archives')
    parser.add_argument('--grace-seconds', type=int, default=0,
                        help='publication-only grace interval (minimum 30)')
    parser.add_argument('--single-build', action='store_true',
                        help='private diagnostics only; forbidden for publish')
    parser.add_argument('--check', action='store_true',
                        help='validate an already published atomic set')
    args = parser.parse_args(argv)
    if args.check:
        if args.publish or args.single_build or args.grace_seconds:
            parser.error('--check cannot be combined with build options')
        check()
        return
    build(publish=args.publish, grace_seconds=args.grace_seconds,
          double_build=not args.single_build)


if __name__ == '__main__':
    main()
