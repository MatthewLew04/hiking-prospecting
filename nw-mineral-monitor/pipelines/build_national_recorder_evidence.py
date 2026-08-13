#!/usr/bin/env python3
"""Build immutable WS11 recorder-coverage evidence for all 19 claim states.

This is an evidence compiler, not a claims fetcher or portal scraper.  It
consumes checksum-pinned, complete active-claim publication inventories,
authoritative jurisdiction polygons, and operator-reviewed portal matrices
from a private staging tree.  Active claims are spatially joined to five-digit
county FIPS jurisdictions in the lower 48 and to Alaska DNR recording-district
names in Alaska.  Alaska's federal and state systems remain separate in the
evidence and their jurisdiction sets are unioned only at the state boundary.

The compiler writes content-addressed state evidence and a content-addressed
run document before atomically replacing ``latest.json``.  It never edits the
state registry, release flags, browser manifest, or map artifacts, and it does
not access the network.
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
from datetime import datetime

from state_registry import CLAIM_STATES, STATES_DIR, load_states, validate_registry


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
SITE = os.path.join(ROOT, 'site')
DATASET = 'ws11-national-recorder-evidence'
SCHEMA_VERSION = 1
SYSTEM_SCOPES = {
    'federal_mlrs': frozenset(CLAIM_STATES),
    'alaska_state_claims': frozenset(('AK',)),
}
SYSTEM_SOURCE_URLS = {
    'federal_mlrs': ('https://gis.blm.gov/nlsdb/rest/services/Mining_Claims/'
                     'MiningClaims/MapServer'),
    'alaska_state_claims': ('https://arcgis.dnr.alaska.gov/arcgis/rest/services/'
                            'OpenData/NaturalResource_StateMiningClaim/FeatureServer'),
}
STATE_FIPS = {
    'AK': '02', 'AZ': '04', 'AR': '05', 'CA': '06', 'CO': '08',
    'FL': '12', 'ID': '16', 'LA': '22', 'MS': '28', 'MT': '30',
    'NE': '31', 'NV': '32', 'NM': '35', 'ND': '38', 'OR': '41',
    'SD': '46', 'UT': '49', 'WA': '53', 'WY': '56',
}
SHA_RE = re.compile(r'[0-9a-f]{64}')
DATE_RE = re.compile(r'\d{4}-\d{2}-\d{2}')
COUNTY_FIPS_RE = re.compile(r'\d{5}')
ID_RE = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}')
GEOMETRY_TYPES = frozenset(('Point', 'MultiPoint', 'Polygon', 'MultiPolygon'))


class PublicationError(ValueError):
    """An input or publication violates the recorder-evidence contract."""


def _reject_constant(value):
    raise ValueError(f'non-standard JSON number {value}')


def _reject_duplicate_keys(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f'duplicate JSON object key {key!r}')
        value[key] = item
    return value


def strict_json_bytes(raw, label):
    try:
        return json.loads(
            raw.decode('utf-8'), parse_constant=_reject_constant,
            object_pairs_hook=_reject_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise PublicationError(f'{label} is not strict UTF-8 JSON: {exc}') from exc


def load_strict_json(path, label):
    try:
        with open(path, 'rb') as source:
            raw = source.read()
    except OSError as exc:
        raise PublicationError(f'cannot read {label}: {exc}') from exc
    return strict_json_bytes(raw, label), raw


def canonical_bytes(value):
    try:
        return json.dumps(
            value, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
            allow_nan=False).encode('utf-8')
    except (TypeError, ValueError) as exc:
        raise PublicationError(f'cannot encode canonical evidence JSON: {exc}') from exc


def sha256_bytes(raw):
    return hashlib.sha256(raw).hexdigest()


def sha256_file(path):
    digest = hashlib.sha256()
    try:
        with open(path, 'rb') as source:
            for chunk in iter(lambda: source.read(1 << 20), b''):
                digest.update(chunk)
    except OSError as exc:
        raise PublicationError(f'cannot hash {path}: {exc}') from exc
    return digest.hexdigest()


def _expect_keys(value, required, optional, label):
    if not isinstance(value, dict):
        raise PublicationError(f'{label} must be an object')
    missing = sorted(set(required) - set(value))
    extra = sorted(set(value) - set(required) - set(optional))
    if missing or extra:
        raise PublicationError(
            f'{label} keys mismatch: missing={missing}, extra={extra}')


def _text(value, label, minimum=1, maximum=4096):
    if (not isinstance(value, str) or value != value.strip() or
            not minimum <= len(value) <= maximum or '\x00' in value or
            '\r' in value or '\n' in value):
        raise PublicationError(
            f'{label} must be trimmed single-line text of length '
            f'{minimum}..{maximum}')
    return value


def _identifier(value, label):
    _text(value, label, maximum=160)
    if ID_RE.fullmatch(value) is None:
        raise PublicationError(f'{label} must be a stable identifier')
    return value


def _date(value, label):
    if not isinstance(value, str) or DATE_RE.fullmatch(value) is None:
        raise PublicationError(f'{label} must be YYYY-MM-DD')
    try:
        datetime.strptime(value, '%Y-%m-%d')
    except ValueError as exc:
        raise PublicationError(f'{label} is not a calendar date') from exc
    return value


def _https(value, label):
    _text(value, label, maximum=2048)
    if re.fullmatch(r'https://[^\s]+', value) is None:
        raise PublicationError(f'{label} must be an HTTPS URL')
    return value


def _count(value, label, *, positive=False):
    if (not isinstance(value, int) or isinstance(value, bool) or value < 0 or
            (positive and value == 0)):
        qualifier = 'positive' if positive else 'nonnegative'
        raise PublicationError(f'{label} must be a {qualifier} integer')
    return value


def _sha(value, label):
    if not isinstance(value, str) or SHA_RE.fullmatch(value) is None:
        raise PublicationError(f'{label} must be a lowercase SHA-256')
    return value


def _inside(path, parent):
    try:
        return os.path.commonpath(
            (os.path.realpath(path), os.path.realpath(parent))) == os.path.realpath(parent)
    except ValueError:
        return False


def _private_file(path, label):
    if _inside(path, SITE):
        raise PublicationError(f'{label} must remain outside public site/')
    if os.path.islink(path):
        raise PublicationError(f'{label} must not be a symlink')
    if not os.path.isfile(path):
        raise PublicationError(f'{label} is missing: {path}')


def _safe_relative(value, label):
    _text(value, label, maximum=500)
    normalized = value.replace('\\', '/')
    if (os.path.isabs(value) or normalized.startswith('/') or
            any(part in ('', '.', '..') for part in normalized.split('/'))):
        raise PublicationError(f'{label} must be a normalized relative path')
    return normalized


@dataclass(frozen=True)
class Artifact:
    label: str
    relative_path: str
    path: str
    bytes: int
    sha256: str


def _artifact_descriptor(value, base, label, *, allow_empty=False):
    _expect_keys(value, ('path', 'bytes', 'sha256'), (), label)
    relative = _safe_relative(value['path'], f'{label}.path')
    path = os.path.realpath(os.path.join(base, relative))
    if not _inside(path, base):
        raise PublicationError(f'{label}.path escapes its staging directory')
    _private_file(path, label)
    size = _count(value['bytes'], f'{label}.bytes', positive=not allow_empty)
    digest = _sha(value['sha256'], f'{label}.sha256')
    if os.path.getsize(path) != size or sha256_file(path) != digest:
        raise PublicationError(f'{label} bytes/sha256 do not match the file')
    return Artifact(label, relative, path, size, digest)


def _nested_artifact(value, base, label, *, allow_empty=False):
    """Validate a publication-owned file descriptor using its ``file`` key."""
    _expect_keys(
        value,
        ('file', 'format', 'n', 'bytes', 'sha256', 'retrieved', 'complete',
         'truncated', 'total_available'), (), label)
    relative = _safe_relative(value['file'], f'{label}.file')
    path = os.path.realpath(os.path.join(base, relative))
    if not _inside(path, base):
        raise PublicationError(f'{label}.file escapes its publication directory')
    _private_file(path, label)
    if value['format'] != 'geojsonseq_v1':
        raise PublicationError(f'{label}.format must be geojsonseq_v1')
    n = _count(value['n'], f'{label}.n')
    size = _count(value['bytes'], f'{label}.bytes')
    digest = _sha(value['sha256'], f'{label}.sha256')
    _date(value['retrieved'], f'{label}.retrieved')
    if (value['complete'] is not True or value['truncated'] is not False or
            value['total_available'] != n):
        raise PublicationError(
            f'{label} must be complete, uncapped, untruncated, and '
            'n == total_available')
    if n and not size:
        raise PublicationError(f'{label} declares rows in an empty file')
    if not n and size and not allow_empty:
        # Empty systems are represented by an empty sequence, eliminating
        # ambiguous header-only or sentinel records.
        raise PublicationError(f'{label} zero-row sequence must be zero bytes')
    if os.path.getsize(path) != size or sha256_file(path) != digest:
        raise PublicationError(f'{label} bytes/sha256 do not match the file')
    return Artifact(label, relative, path, size, digest)


def _registry_context():
    validation = validate_registry()
    if not validation['ok']:
        raise PublicationError(
            'state registry is invalid: ' + '; '.join(validation['errors']))
    rows = load_states()
    codes = {code for code, row in rows.items() if row.get('regime') == 'claim'}
    if codes != set(CLAIM_STATES) or len(codes) != 19:
        raise PublicationError('registry claim scope is not the exact WS11 19')
    context = {}
    for code in sorted(codes):
        system_ids = [
            item.get('id') for item in rows[code].get('claim_systems', [])
            if isinstance(item, dict) and isinstance(item.get('id'), str)
        ]
        expected = ['federal_mlrs', 'alaska_state_claims'] if code == 'AK' else [
            'federal_mlrs']
        if system_ids != expected:
            raise PublicationError(
                f'{code} registry claim systems must be {expected!r} in that order')
        configured_type = (rows[code].get('recorder') or {}).get(
            'jurisdiction_type')
        expected_type = 'recording_district' if code == 'AK' else 'county'
        if configured_type != expected_type:
            raise PublicationError(
                f'{code} registry recorder jurisdiction must be {expected_type}')
        context[code] = {
            'jurisdiction_type': expected_type,
            'claim_systems': system_ids,
        }
    registry_files = []
    for path in [os.path.join(STATES_DIR, '_defaults.yaml')] + [
            os.path.join(STATES_DIR, f'{code}.yaml') for code in sorted(codes)]:
        registry_files.append((path, os.path.getsize(path), sha256_file(path)))
    digest = sha256_bytes(canonical_bytes(context))
    return context, digest, registry_files


def _number(value, label):
    if (not isinstance(value, (int, float)) or isinstance(value, bool) or
            not math.isfinite(value)):
        raise PublicationError(f'{label} must be a finite coordinate')
    result = float(value)
    if not -180 <= result <= 180:
        raise PublicationError(f'{label} is outside the coordinate domain')
    return result


def _position(value, label):
    if not isinstance(value, list) or len(value) not in (2, 3):
        raise PublicationError(f'{label} must be a two- or three-number position')
    x = _number(value[0], f'{label}[0]')
    y = _number(value[1], f'{label}[1]')
    if not -90 <= y <= 90:
        raise PublicationError(f'{label}[1] is outside latitude range')
    if len(value) == 3:
        altitude = value[2]
        if (not isinstance(altitude, (int, float)) or isinstance(altitude, bool) or
                not math.isfinite(altitude)):
            raise PublicationError(f'{label}[2] must be finite')
    return x, y


def _ring(value, label):
    if not isinstance(value, list) or len(value) < 4:
        raise PublicationError(f'{label} must contain at least four positions')
    points = [_position(item, f'{label}[{index}]')
              for index, item in enumerate(value)]
    if points[0] != points[-1]:
        raise PublicationError(f'{label} must be closed')
    if len(set(points[:-1])) < 3:
        raise PublicationError(f'{label} must contain three distinct vertices')
    return points


def validate_geometry(value, label, *, jurisdiction=False):
    _expect_keys(value, ('type', 'coordinates'), (), label)
    kind = value['type']
    allowed = frozenset(('Polygon', 'MultiPolygon')) if jurisdiction else GEOMETRY_TYPES
    if kind not in allowed:
        raise PublicationError(f'{label}.type must be one of {sorted(allowed)}')
    coordinates = value['coordinates']
    if kind == 'Point':
        normalized = _position(coordinates, f'{label}.coordinates')
    elif kind == 'MultiPoint':
        if not isinstance(coordinates, list) or not coordinates:
            raise PublicationError(f'{label}.coordinates must be a nonempty list')
        normalized = [_position(item, f'{label}.coordinates[{index}]')
                      for index, item in enumerate(coordinates)]
    elif kind == 'Polygon':
        if not isinstance(coordinates, list) or not coordinates:
            raise PublicationError(f'{label}.coordinates must contain rings')
        normalized = [_ring(item, f'{label}.coordinates[{index}]')
                      for index, item in enumerate(coordinates)]
    else:
        if not isinstance(coordinates, list) or not coordinates:
            raise PublicationError(f'{label}.coordinates must contain polygons')
        normalized = []
        for poly_index, polygon in enumerate(coordinates):
            if not isinstance(polygon, list) or not polygon:
                raise PublicationError(
                    f'{label}.coordinates[{poly_index}] must contain rings')
            normalized.append([
                _ring(ring, f'{label}.coordinates[{poly_index}][{ring_index}]')
                for ring_index, ring in enumerate(polygon)
            ])
    return {'type': kind, 'coordinates': normalized}


def _points(geometry):
    kind, coordinates = geometry['type'], geometry['coordinates']
    if kind == 'Point':
        yield coordinates
    elif kind == 'MultiPoint':
        yield from coordinates
    elif kind == 'Polygon':
        for ring in coordinates:
            yield from ring[:-1]
    else:
        for polygon in coordinates:
            for ring in polygon:
                yield from ring[:-1]


def _polygons(geometry):
    if geometry['type'] == 'Polygon':
        return [geometry['coordinates']]
    if geometry['type'] == 'MultiPolygon':
        return geometry['coordinates']
    return []


def _bbox(geometry):
    points = list(_points(geometry))
    return (min(point[0] for point in points), min(point[1] for point in points),
            max(point[0] for point in points), max(point[1] for point in points))


def _bbox_intersects(left, right):
    return not (left[2] < right[0] or left[0] > right[2] or
                left[3] < right[1] or left[1] > right[3])


def _point_on_segment(point, left, right, epsilon=1e-10):
    px, py = point
    ax, ay = left
    bx, by = right
    cross = (px - ax) * (by - ay) - (py - ay) * (bx - ax)
    scale = max(1.0, abs(bx - ax), abs(by - ay))
    if abs(cross) > epsilon * scale:
        return False
    return (min(ax, bx) - epsilon <= px <= max(ax, bx) + epsilon and
            min(ay, by) - epsilon <= py <= max(ay, by) + epsilon)


def _ring_relation(point, ring):
    inside = False
    x, y = point
    for index in range(len(ring) - 1):
        left, right = ring[index], ring[index + 1]
        if _point_on_segment(point, left, right):
            return 'boundary'
        x1, y1 = left
        x2, y2 = right
        if ((y1 > y) != (y2 > y) and
                x < (x2 - x1) * (y - y1) / (y2 - y1) + x1):
            inside = not inside
    return 'inside' if inside else 'outside'


def _polygon_covers(point, polygon):
    exterior = _ring_relation(point, polygon[0])
    if exterior == 'outside':
        return False
    if exterior == 'boundary':
        return True
    for hole in polygon[1:]:
        relation = _ring_relation(point, hole)
        if relation == 'boundary':
            return True
        if relation == 'inside':
            return False
    return True


def geometry_covers_point(geometry, point):
    return any(_polygon_covers(point, polygon) for polygon in _polygons(geometry))


def _orientation(left, middle, right, epsilon=1e-12):
    value = ((middle[1] - left[1]) * (right[0] - middle[0]) -
             (middle[0] - left[0]) * (right[1] - middle[1]))
    if abs(value) <= epsilon:
        return 0
    return 1 if value > 0 else 2


def _segments_intersect(a, b, c, d):
    o1, o2 = _orientation(a, b, c), _orientation(a, b, d)
    o3, o4 = _orientation(c, d, a), _orientation(c, d, b)
    if o1 != o2 and o3 != o4:
        return True
    return ((o1 == 0 and _point_on_segment(c, a, b)) or
            (o2 == 0 and _point_on_segment(d, a, b)) or
            (o3 == 0 and _point_on_segment(a, c, d)) or
            (o4 == 0 and _point_on_segment(b, c, d)))


def _segments(geometry):
    for polygon in _polygons(geometry):
        for ring in polygon:
            for index in range(len(ring) - 1):
                yield ring[index], ring[index + 1]


def geometries_intersect(claim, jurisdiction):
    if not _bbox_intersects(_bbox(claim), _bbox(jurisdiction)):
        return False
    if claim['type'] in ('Point', 'MultiPoint'):
        return any(geometry_covers_point(jurisdiction, point)
                   for point in _points(claim))
    if any(geometry_covers_point(jurisdiction, point) for point in _points(claim)):
        return True
    if any(geometry_covers_point(claim, point) for point in _points(jurisdiction)):
        return True
    for left_a, right_a in _segments(claim):
        segment_bbox = (min(left_a[0], right_a[0]), min(left_a[1], right_a[1]),
                        max(left_a[0], right_a[0]), max(left_a[1], right_a[1]))
        for left_b, right_b in _segments(jurisdiction):
            other_bbox = (min(left_b[0], right_b[0]), min(left_b[1], right_b[1]),
                          max(left_b[0], right_b[0]), max(left_b[1], right_b[1]))
            if (_bbox_intersects(segment_bbox, other_bbox) and
                    _segments_intersect(left_a, right_a, left_b, right_b)):
                return True
    return False


class JurisdictionIndex:
    """Small bbox grid over one state's authoritative recorder polygons."""

    def __init__(self, rows, cell=1.0):
        self.rows = rows
        self.cell = cell
        self.grid = {}
        for index, row in enumerate(rows):
            west, south, east, north = row['bbox']
            for x in range(math.floor(west / cell), math.floor(east / cell) + 1):
                for y in range(math.floor(south / cell), math.floor(north / cell) + 1):
                    self.grid.setdefault((x, y), set()).add(index)

    def match(self, geometry):
        west, south, east, north = _bbox(geometry)
        candidates = set()
        for x in range(math.floor(west / self.cell), math.floor(east / self.cell) + 1):
            for y in range(math.floor(south / self.cell), math.floor(north / self.cell) + 1):
                candidates.update(self.grid.get((x, y), ()))
        return sorted(
            self.rows[index]['jurisdiction_id'] for index in candidates
            if geometries_intersect(geometry, self.rows[index]['geometry']))


def _validate_state_clips(document):
    _expect_keys(
        document, ('schema_version', 'source', 'states'), ('note',),
        'state clips')
    if document['schema_version'] != 1:
        raise PublicationError('state clips schema_version must be 1')
    source = _text(document['source'], 'state clips.source', maximum=2048)
    if 'tigerweb' not in source.lower() or 'January 1 2025' not in source:
        raise PublicationError(
            'state clips must identify the reviewed Census TIGERweb 2025 source')
    states = document['states']
    all_states = set(load_states())
    if not isinstance(states, dict) or set(states) != all_states or len(states) != 49:
        raise PublicationError('state clips must contain the exact WS11 49 states')
    clips = {}
    for code in sorted(CLAIM_STATES):
        clips[code] = validate_geometry(
            states[code], f'state clips.states.{code}', jurisdiction=True)
    return clips


def _jurisdiction_id(value, code, kind, label):
    _text(value, label, maximum=100)
    if kind == 'county':
        if (COUNTY_FIPS_RE.fullmatch(value) is None or
                value[:2] != STATE_FIPS[code]):
            raise PublicationError(
                f'{label} must be a five-digit {code} county FIPS')
    elif kind == 'recording_district':
        if code != 'AK' or value.isdigit():
            raise PublicationError(
                f'{label} must be an Alaska DNR recording-district name')
    else:
        raise PublicationError(f'{label} has unsupported jurisdiction type {kind}')
    return value


def _validate_jurisdictions(document, code, expected_type, state_clip, snapshot):
    _expect_keys(
        document,
        ('schema_version', 'type', 'state', 'jurisdiction_type', 'authority',
         'official_url', 'retrieved', 'complete', 'features'), (),
        f'{code} jurisdiction polygons')
    if (document['schema_version'] != 1 or document['type'] != 'FeatureCollection' or
            document['state'] != code or
            document['jurisdiction_type'] != expected_type or
            document['complete'] is not True):
        raise PublicationError(f'{code} jurisdiction polygon identity is invalid')
    _text(document['authority'], f'{code} jurisdictions.authority', maximum=500)
    _https(document['official_url'], f'{code} jurisdictions.official_url')
    retrieved = _date(document['retrieved'], f'{code} jurisdictions.retrieved')
    if retrieved > snapshot:
        raise PublicationError(f'{code} jurisdictions are dated after the run snapshot')
    features = document['features']
    if not isinstance(features, list) or not features:
        raise PublicationError(f'{code} jurisdiction features must be nonempty')
    rows, identifiers = [], set()
    for index, feature in enumerate(features):
        label = f'{code} jurisdictions.features[{index}]'
        _expect_keys(feature, ('type', 'properties', 'geometry'), (), label)
        if feature['type'] != 'Feature':
            raise PublicationError(f'{label}.type must be Feature')
        properties = feature['properties']
        _expect_keys(properties, ('jurisdiction_id', 'name'), (), f'{label}.properties')
        identifier = _jurisdiction_id(
            properties['jurisdiction_id'], code, expected_type,
            f'{label}.properties.jurisdiction_id')
        name = _text(properties['name'], f'{label}.properties.name', maximum=200)
        if expected_type == 'recording_district' and name != identifier:
            raise PublicationError(
                f'{label} Alaska jurisdiction_id must be the reviewed district name')
        if identifier in identifiers:
            raise PublicationError(f'{code} duplicate jurisdiction {identifier}')
        identifiers.add(identifier)
        geometry = validate_geometry(feature['geometry'], f'{label}.geometry',
                                     jurisdiction=True)
        # Reject a mislabeled or wholly off-state jurisdiction source.  Claims
        # are checked more strictly, point by point, during the streaming join.
        if not any(geometry_covers_point(state_clip, point)
                   for point in _points(geometry)):
            raise PublicationError(
                f'{code} jurisdiction {identifier} has no vertex in its state clip')
        rows.append({'jurisdiction_id': identifier, 'name': name,
                     'geometry': geometry, 'bbox': _bbox(geometry)})
    return rows


def _validate_matrix(document, code, expected_type, jurisdiction_ids, snapshot):
    _expect_keys(
        document,
        ('schema_version', 'state', 'jurisdiction_type', 'status', 'reviewed_on',
         'reviewed_by', 'complete', 'official_directory_url', 'rows'), (),
        f'{code} portal matrix')
    if (document['schema_version'] != 1 or document['state'] != code or
            document['jurisdiction_type'] != expected_type or
            document['status'] != 'reviewed' or document['complete'] is not True):
        raise PublicationError(f'{code} portal matrix review identity is invalid')
    reviewed = _date(document['reviewed_on'], f'{code} matrix.reviewed_on')
    if reviewed > snapshot:
        raise PublicationError(f'{code} portal matrix is dated after the run snapshot')
    _text(document['reviewed_by'], f'{code} matrix.reviewed_by', maximum=200)
    _https(document['official_directory_url'],
           f'{code} matrix.official_directory_url')
    rows = document['rows']
    if not isinstance(rows, list):
        raise PublicationError(f'{code} portal matrix rows must be a list')
    result = {}
    for index, row in enumerate(rows):
        label = f'{code} matrix.rows[{index}]'
        _expect_keys(
            row,
            ('jurisdiction_id', 'status', 'portal_vendor', 'portal_url',
             'official_url'), (), label)
        identifier = _jurisdiction_id(
            row['jurisdiction_id'], code, expected_type,
            f'{label}.jurisdiction_id')
        if identifier not in jurisdiction_ids:
            raise PublicationError(
                f'{label}.jurisdiction_id is absent from authoritative polygons')
        if identifier in result:
            raise PublicationError(f'{code} duplicate portal row {identifier}')
        if row['status'] != 'accepted':
            raise PublicationError(f'{label}.status must be accepted')
        _text(row['portal_vendor'], f'{label}.portal_vendor', maximum=200)
        _https(row['portal_url'], f'{label}.portal_url')
        _https(row['official_url'], f'{label}.official_url')
        result[identifier] = row
    return result


def _load_publication(artifact, system_id, snapshot):
    document, raw = load_strict_json(artifact.path, f'{system_id} publication inventory')
    if sha256_bytes(raw) != artifact.sha256:
        raise PublicationError(f'{system_id} publication inventory changed while loading')
    _expect_keys(
        document,
        ('schema_version', 'system_id', 'source_url', 'created', 'states'), (),
        f'{system_id} publication inventory')
    if document['schema_version'] != 1 or document['system_id'] != system_id:
        raise PublicationError(f'{system_id} publication inventory identity is invalid')
    _https(document['source_url'], f'{system_id} publication source_url')
    if document['source_url'] != SYSTEM_SOURCE_URLS[system_id]:
        raise PublicationError(
            f'{system_id} publication source_url is not the registered authority')
    created = _date(document['created'], f'{system_id} publication created')
    if created > snapshot:
        raise PublicationError(f'{system_id} publication is dated after the run snapshot')
    states = document['states']
    expected = set(SYSTEM_SCOPES[system_id])
    if not isinstance(states, dict) or set(states) != expected:
        raise PublicationError(
            f'{system_id} publication state scope must be exactly {sorted(expected)}')
    active = {}
    for code in sorted(expected):
        row = states[code]
        _expect_keys(row, ('active',), (), f'{system_id}.states.{code}')
        descriptor = row['active']
        active[code] = {
            'artifact': _nested_artifact(
                descriptor, os.path.dirname(artifact.path),
                f'{system_id}.{code}.active'),
            'n': descriptor['n'],
            'retrieved': descriptor['retrieved'],
        }
        if descriptor['retrieved'] > snapshot:
            raise PublicationError(
                f'{system_id}.{code}.active is dated after the run snapshot')
    return document, active


def load_inventory(inventory_path):
    """Load and checksum the complete private national recorder input set."""
    inventory_path = os.path.realpath(inventory_path)
    _private_file(inventory_path, 'recorder input inventory')
    inventory, inventory_raw = load_strict_json(
        inventory_path, 'recorder input inventory')
    _expect_keys(
        inventory,
        ('schema_version', 'dataset', 'snapshot', 'state_clips', 'publications',
         'jurisdictions', 'portal_matrices'), (), 'recorder input inventory')
    if (inventory['schema_version'] != 1 or inventory['dataset'] != DATASET):
        raise PublicationError(
            f'recorder inventory must be schema 1 and dataset={DATASET!r}')
    snapshot = _date(inventory['snapshot'], 'recorder inventory.snapshot')
    base = os.path.dirname(inventory_path)
    registry, registry_sha, registry_files = _registry_context()
    codes = set(registry)
    publications = inventory['publications']
    if not isinstance(publications, dict) or set(publications) != set(SYSTEM_SCOPES):
        raise PublicationError('publications must contain federal_mlrs and '
                               'alaska_state_claims exactly')
    tracked = []
    publication_context = {}
    for system_id in sorted(SYSTEM_SCOPES):
        artifact = _artifact_descriptor(
            publications[system_id], base, f'{system_id} publication inventory')
        tracked.append(artifact)
        document, active = _load_publication(artifact, system_id, snapshot)
        tracked.extend(row['artifact'] for row in active.values())
        publication_context[system_id] = {
            'artifact': artifact, 'document': document, 'active': active,
        }
    clip_artifact = _artifact_descriptor(
        inventory['state_clips'], base, 'authoritative state clips')
    tracked.append(clip_artifact)
    clip_document, _ = load_strict_json(clip_artifact.path, 'authoritative state clips')
    state_clips = _validate_state_clips(clip_document)
    jurisdictions = inventory['jurisdictions']
    matrices = inventory['portal_matrices']
    if (not isinstance(jurisdictions, dict) or set(jurisdictions) != codes or
            not isinstance(matrices, dict) or set(matrices) != codes):
        raise PublicationError(
            'jurisdictions and portal_matrices must each contain exact claim-state 19')
    state_context = {}
    for code in sorted(codes):
        jurisdiction_artifact = _artifact_descriptor(
            jurisdictions[code], base, f'{code} jurisdiction polygons')
        matrix_artifact = _artifact_descriptor(
            matrices[code], base, f'{code} portal matrix')
        tracked.extend((jurisdiction_artifact, matrix_artifact))
        jurisdiction_document, _ = load_strict_json(
            jurisdiction_artifact.path, f'{code} jurisdiction polygons')
        rows = _validate_jurisdictions(
            jurisdiction_document, code, registry[code]['jurisdiction_type'],
            state_clips[code], snapshot)
        matrix_document, _ = load_strict_json(
            matrix_artifact.path, f'{code} portal matrix')
        matrix = _validate_matrix(
            matrix_document, code, registry[code]['jurisdiction_type'],
            {row['jurisdiction_id'] for row in rows}, snapshot)
        state_context[code] = {
            'jurisdiction_artifact': jurisdiction_artifact,
            'matrix_artifact': matrix_artifact,
            'jurisdictions': rows,
            'index': JurisdictionIndex(rows),
            'matrix': matrix,
            'state_clip': state_clips[code],
        }
    return {
        'inventory_path': inventory_path,
        'inventory_bytes': len(inventory_raw),
        'inventory_sha256': sha256_bytes(inventory_raw),
        'inventory': inventory,
        'snapshot': snapshot,
        'registry': registry,
        'registry_sha256': registry_sha,
        'registry_files': registry_files,
        'publications': publication_context,
        'state_clips_artifact': clip_artifact,
        'states': state_context,
        'tracked_artifacts': tracked,
    }


def _strict_sequence_feature(raw, label, code, system_id):
    if raw.startswith(b'\x1e'):
        raw = raw[1:]
    feature = strict_json_bytes(raw, label)
    _expect_keys(feature, ('type', 'id', 'properties', 'geometry'), (), label)
    if feature['type'] != 'Feature':
        raise PublicationError(f'{label}.type must be Feature')
    feature_id = feature['id']
    if isinstance(feature_id, bool) or not isinstance(feature_id, (str, int)):
        raise PublicationError(f'{label}.id must be a string or integer')
    if isinstance(feature_id, str):
        _identifier(feature_id, f'{label}.id')
        feature_key = 's:' + feature_id
    else:
        if not -(1 << 63) <= feature_id < (1 << 63):
            raise PublicationError(f'{label}.id is outside signed 64-bit range')
        feature_key = 'i:' + str(feature_id)
    properties = feature['properties']
    if not isinstance(properties, dict):
        raise PublicationError(f'{label}.properties must be an object')
    required = {'claim_id', 'st', 'system_id', 'status'}
    if not required <= set(properties):
        raise PublicationError(
            f'{label}.properties is missing {sorted(required - set(properties))}')
    _text(properties['claim_id'], f'{label}.properties.claim_id', maximum=300)
    if (properties['st'] != code or properties['system_id'] != system_id or
            properties['status'] != 'active'):
        raise PublicationError(f'{label} active claim identity is invalid')
    geometry = validate_geometry(feature['geometry'], f'{label}.geometry')
    return feature_key, geometry


def _claim_is_in_state(geometry, state_clip):
    # Every declared vertex must be in or on the authoritative state polygon.
    # Segment midpoints catch the common concave-boundary crossing case while
    # preserving claims whose vertices lie exactly on a legal state boundary.
    for point in _points(geometry):
        if not geometry_covers_point(state_clip, point):
            return False
    if geometry['type'] in ('Polygon', 'MultiPolygon'):
        for left, right in _segments(geometry):
            midpoint = ((left[0] + right[0]) / 2, (left[1] + right[1]) / 2)
            if not geometry_covers_point(state_clip, midpoint):
                return False
    return True


def _join_active(context, code, system_id):
    active = context['publications'][system_id]['active'][code]
    artifact = active['artifact']
    seen = set()
    jurisdictions = set()
    count = 0
    try:
        with open(artifact.path, 'rb') as source:
            for line_number, raw in enumerate(source, 1):
                if not raw.endswith(b'\n'):
                    raise PublicationError(
                        f'{system_id}.{code}.active line {line_number} lacks newline')
                payload = raw[:-1]
                if payload.endswith(b'\r'):
                    payload = payload[:-1]
                if not payload:
                    raise PublicationError(
                        f'{system_id}.{code}.active has a blank record at line '
                        f'{line_number}')
                label = f'{system_id}.{code}.active[{line_number - 1}]'
                feature_key, geometry = _strict_sequence_feature(
                    payload, label, code, system_id)
                if feature_key in seen:
                    raise PublicationError(
                        f'{system_id}.{code}.active duplicate feature id '
                        f'{feature_key[2:]}')
                seen.add(feature_key)
                if not _claim_is_in_state(
                        geometry, context['states'][code]['state_clip']):
                    raise PublicationError(
                        f'{label} is off-state under the authoritative state clip')
                matched = context['states'][code]['index'].match(geometry)
                if not matched:
                    raise PublicationError(
                        f'{label} is unmapped by authoritative recorder jurisdictions')
                jurisdictions.update(matched)
                count += 1
    except OSError as exc:
        raise PublicationError(
            f'cannot stream {system_id}.{code}.active: {exc}') from exc
    if count != active['n']:
        raise PublicationError(
            f'{system_id}.{code}.active has {count} features; inventory declares '
            f'{active["n"]}')
    if os.path.getsize(artifact.path) != artifact.bytes or sha256_file(
            artifact.path) != artifact.sha256:
        raise PublicationError(f'{system_id}.{code}.active changed during join')
    return count, sorted(jurisdictions)


def _verify_unchanged(context):
    path = context['inventory_path']
    if (os.path.getsize(path) != context['inventory_bytes'] or
            sha256_file(path) != context['inventory_sha256']):
        raise PublicationError('recorder input inventory changed during build')
    for artifact in context['tracked_artifacts']:
        try:
            size = os.path.getsize(artifact.path)
            digest = sha256_file(artifact.path)
        except OSError as exc:
            raise PublicationError(
                f'{artifact.label} changed/disappeared during build: {exc}') from exc
        if size != artifact.bytes or digest != artifact.sha256:
            raise PublicationError(f'{artifact.label} changed during build')
    for path, size, digest in context['registry_files']:
        if os.path.getsize(path) != size or sha256_file(path) != digest:
            raise PublicationError('claim-state registry changed during build')


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
        try:
            with open(path, 'rb') as existing:
                if existing.read() != raw:
                    raise PublicationError(
                        f'content-addressed path collision at {path}')
        except OSError as exc:
            raise PublicationError(f'cannot inspect immutable artifact {path}: {exc}') from exc
        return
    _atomic_write(path, raw)


def _relative_json(value, label):
    relative = _safe_relative(value, label)
    if not relative.endswith('.json'):
        raise PublicationError(f'{label} must end in .json')
    return relative


def validate_pointer(publish_dir):
    """Validate a published pointer, run, and all 19 state evidence files."""
    publish_dir = os.path.realpath(publish_dir)
    pointer, pointer_raw = load_strict_json(
        os.path.join(publish_dir, 'latest.json'), 'recorder latest pointer')
    _expect_keys(
        pointer,
        ('schema_version', 'dataset', 'snapshot', 'run', 'sha256', 'states',
         'inventory_complete', 'effect'), (), 'recorder latest pointer')
    if (pointer['schema_version'] != 1 or pointer['dataset'] != DATASET or
            pointer['states'] != 19 or pointer['inventory_complete'] is not True or
            pointer['effect'] != 'evidence_only_no_release_mutation'):
        raise PublicationError('recorder latest pointer identity is invalid')
    _date(pointer['snapshot'], 'recorder latest pointer.snapshot')
    run_relative = _relative_json(pointer['run'], 'recorder latest pointer.run')
    run_sha = _sha(pointer['sha256'], 'recorder latest pointer.sha256')
    run_path = os.path.join(publish_dir, run_relative)
    if sha256_file(run_path) != run_sha:
        raise PublicationError('recorder latest pointer run checksum mismatch')
    run, _ = load_strict_json(run_path, 'recorder run evidence')
    _expect_keys(
        run,
        ('schema_version', 'dataset', 'snapshot', 'states', 'registry_sha256',
         'inventory', 'state_clips', 'publications', 'state_evidence',
         'national_metrics', 'inventory_complete', 'effect'), (),
        'recorder run evidence')
    registry, registry_sha, _registry_files = _registry_context()
    if (run['schema_version'] != 1 or run['dataset'] != DATASET or
            run['snapshot'] != pointer['snapshot'] or run['states'] != 19 or
            run['registry_sha256'] != registry_sha or
            run['inventory_complete'] is not True or
            run['effect'] != 'evidence_only_no_release_mutation'):
        raise PublicationError('recorder run identity is invalid')
    references = run['state_evidence']
    if not isinstance(references, dict) or set(references) != set(registry):
        raise PublicationError('recorder run must reference exact claim-state 19')
    for code in sorted(registry):
        descriptor = references[code]
        _expect_keys(
            descriptor,
            ('file', 'bytes', 'sha256', 'jurisdiction_type', 'active_claims',
             'live_jurisdictions', 'claim_systems'), (),
            f'recorder run.state_evidence.{code}')
        evidence_path = os.path.join(
            publish_dir, _relative_json(
                descriptor['file'], f'{code} recorder evidence file'))
        evidence_sha = _sha(descriptor['sha256'], f'{code} recorder evidence sha256')
        if (os.path.getsize(evidence_path) != descriptor['bytes'] or
                sha256_file(evidence_path) != evidence_sha):
            raise PublicationError(f'{code} recorder evidence checksum mismatch')
        evidence, _ = load_strict_json(evidence_path, f'{code} recorder evidence')
        expected_keys = {
            'schema_version', 'state', 'jurisdiction_type', 'inventory_complete',
            'active_claims',
            'live_claim_jurisdiction_ids', 'covered_jurisdiction_ids',
            'matrix_jurisdiction_ids', 'claim_systems',
        }
        if set(evidence) != expected_keys:
            raise PublicationError(f'{code} recorder evidence keys are not schema v1')
        live = evidence.get('live_claim_jurisdiction_ids')
        systems = evidence.get('claim_systems')
        active_claims = evidence.get('active_claims')
        if (evidence.get('schema_version') != 1 or evidence.get('state') != code or
                evidence.get('jurisdiction_type') !=
                registry[code]['jurisdiction_type'] or
                evidence.get('inventory_complete') is not True or
                not isinstance(active_claims, int) or isinstance(active_claims, bool) or
                active_claims < 0 or not isinstance(live, list) or
                live != sorted(set(live)) or
                ((active_claims == 0) != (live == [])) or
                evidence.get('covered_jurisdiction_ids') != live or
                evidence.get('matrix_jurisdiction_ids') != live or
                not isinstance(systems, list) or
                [item.get('system_id') for item in systems
                 if isinstance(item, dict)] != registry[code]['claim_systems'] or
                any(set(item) != {
                    'system_id', 'active_claims', 'live_claim_jurisdiction_ids'}
                    for item in systems if isinstance(item, dict)) or
                any(not isinstance(item.get('active_claims'), int) or
                    isinstance(item.get('active_claims'), bool) or
                    item['active_claims'] < 0
                    for item in systems if isinstance(item, dict)) or
                sum(item['active_claims'] for item in systems) != active_claims or
                set().union(*(set(item['live_claim_jurisdiction_ids'])
                              for item in systems)) != set(live)):
            raise PublicationError(f'{code} recorder evidence content is invalid')
        if (descriptor['jurisdiction_type'] != evidence['jurisdiction_type'] or
                descriptor['live_jurisdictions'] != len(live) or
                descriptor['claim_systems'] != registry[code]['claim_systems'] or
                descriptor['active_claims'] != active_claims):
            raise PublicationError(f'{code} recorder run descriptor is invalid')
    return {'pointer': pointer, 'pointer_bytes': len(pointer_raw), 'run': run}


def build(inventory_path, publish_dir, before_commit=None):
    """Join all active systems and publish one immutable national evidence run."""
    context = load_inventory(inventory_path)
    publish_dir = os.path.realpath(publish_dir)
    staging_root = os.path.dirname(context['inventory_path'])
    if _inside(publish_dir, staging_root):
        raise PublicationError('publication directory must be outside private staging')
    if os.path.islink(publish_dir):
        raise PublicationError('publication directory must not be a symlink')
    state_documents = {}
    state_references = {}
    national_active = 0
    national_jurisdictions = 0
    system_counts = {system_id: 0 for system_id in SYSTEM_SCOPES}
    for code in sorted(context['registry']):
        system_rows = []
        state_union = set()
        state_active = 0
        for system_id in context['registry'][code]['claim_systems']:
            count, jurisdiction_ids = _join_active(context, code, system_id)
            state_active += count
            system_counts[system_id] += count
            state_union.update(jurisdiction_ids)
            system_rows.append({
                'system_id': system_id,
                'active_claims': count,
                'live_claim_jurisdiction_ids': jurisdiction_ids,
            })
        if (state_active == 0) != (not state_union):
            raise PublicationError(
                f'{code} active claim count and jurisdiction union disagree')
        missing_portals = sorted(
            state_union - set(context['states'][code]['matrix']))
        if missing_portals:
            raise PublicationError(
                f'{code} live jurisdictions lack accepted portal vendor and official '
                f'URL: {missing_portals}')
        live = sorted(state_union)
        evidence = {
            'schema_version': 1,
            'state': code,
            'jurisdiction_type': context['registry'][code]['jurisdiction_type'],
            'inventory_complete': True,
            'active_claims': state_active,
            'live_claim_jurisdiction_ids': live,
            'covered_jurisdiction_ids': live,
            'matrix_jurisdiction_ids': live,
            'claim_systems': system_rows,
        }
        raw = canonical_bytes(evidence)
        digest = sha256_bytes(raw)
        relative = f'states/{code.lower()}/{digest}.json'
        state_documents[code] = (raw, relative)
        state_references[code] = {
            'file': relative,
            'bytes': len(raw),
            'sha256': digest,
            'jurisdiction_type': evidence['jurisdiction_type'],
            'active_claims': state_active,
            'live_jurisdictions': len(live),
            'claim_systems': context['registry'][code]['claim_systems'],
        }
        national_active += state_active
        national_jurisdictions += len(live)
    publication_provenance = {}
    for system_id, publication in sorted(context['publications'].items()):
        publication_provenance[system_id] = {
            'inventory_path': publication['artifact'].relative_path,
            'inventory_bytes': publication['artifact'].bytes,
            'inventory_sha256': publication['artifact'].sha256,
            'active_claims': system_counts[system_id],
            'states': sorted(SYSTEM_SCOPES[system_id]),
        }
    run = {
        'schema_version': 1,
        'dataset': DATASET,
        'snapshot': context['snapshot'],
        'states': 19,
        'registry_sha256': context['registry_sha256'],
        'inventory': {
            'bytes': context['inventory_bytes'],
            'sha256': context['inventory_sha256'],
        },
        'state_clips': {
            'path': context['state_clips_artifact'].relative_path,
            'bytes': context['state_clips_artifact'].bytes,
            'sha256': context['state_clips_artifact'].sha256,
        },
        'publications': publication_provenance,
        'state_evidence': state_references,
        'national_metrics': {
            'active_claim_features': national_active,
            'live_jurisdiction_state_pairs': national_jurisdictions,
            'claim_systems': 20,
        },
        'inventory_complete': True,
        'effect': 'evidence_only_no_release_mutation',
    }
    run_raw = canonical_bytes(run)
    run_sha = sha256_bytes(run_raw)
    run_relative = f'runs/{run_sha}.json'
    pointer = {
        'schema_version': 1,
        'dataset': DATASET,
        'snapshot': context['snapshot'],
        'run': run_relative,
        'sha256': run_sha,
        'states': 19,
        'inventory_complete': True,
        'effect': 'evidence_only_no_release_mutation',
    }
    if before_commit is not None:
        before_commit(context)
    _verify_unchanged(context)
    for code in sorted(state_documents):
        raw, relative = state_documents[code]
        _install_immutable(os.path.join(publish_dir, relative), raw)
    _install_immutable(os.path.join(publish_dir, run_relative), run_raw)
    _verify_unchanged(context)
    _atomic_write(os.path.join(publish_dir, 'latest.json'), canonical_bytes(pointer))
    checked = validate_pointer(publish_dir)
    if checked['pointer'] != pointer or checked['run'] != run:
        raise PublicationError('post-publication pointer verification disagrees')
    return {
        'pointer': 'latest.json',
        'run': run_relative,
        'run_sha256': run_sha,
        'states': 19,
        'active_claim_features': national_active,
        'live_jurisdiction_state_pairs': national_jurisdictions,
        'effect': 'evidence_only_no_release_mutation',
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inventory', required=True,
                        help='private checksum-pinned national input inventory')
    parser.add_argument('--publish', required=True,
                        help='content-addressed evidence output directory')
    parser.add_argument('--validate-only', action='store_true',
                        help='validate an existing --publish pointer and exit')
    args = parser.parse_args(argv)
    try:
        result = (validate_pointer(args.publish) if args.validate_only else
                  build(args.inventory, args.publish))
    except (OSError, PublicationError) as exc:
        parser.exit(1, f'recorder evidence publication failed: {exc}\n')
    print(json.dumps(result, sort_keys=True, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
