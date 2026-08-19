#!/usr/bin/env python3
"""Leapfrog Geo export — package an AOI's research bundles as files Leapfrog
imports natively, so map targets can move straight into 3-D implicit modeling.

Design rules (matching the rest of the repo — see ASSUMPTIONS.md):
- stdlib only; no GDAL, no pyproj, no numpy. Everything Leapfrog needs is
  written byte-for-byte here: ESRI shapefiles, Arc/Info ASCII grids, and
  Open Mining Format v0.9 (validated round-trip against the reference
  `omf` 1.0.1 reader).
- every remote fetch (terrain tiles) is cached under pipelines/cache/terrain/
  and the export degrades honestly: no network -> elevations are written as 0
  and the README the export ships says so. A guessed elevation is never
  presented as a measured one.
- coordinates are exported in a projected CRS (WGS84 / UTM, zone chosen from
  the AOI centroid) because Leapfrog works in a Cartesian XYZ space —
  importing raw lon/lat degrees there produces a uselessly flat pancake.

What lands in the output folder (default exports/leapfrog/<aoi>/):
  mines_grades.csv        graded mines (grades.json rows inside the AOI bbox)
                          East,North,Elev + per-commodity columns -> Leapfrog
                          "Points" import (or drillhole-collar style table)
  targets.csv             WS6 scored exploration targets as points
  claims_active.csv/.shp  BLM claim centroids w/ serial, disposition, acres
  claims_closed.csv/.shp
  geology_units.shp       harmonized geologic-map polygons (name, age, lith)
  faults.shp              mapped structures -> "GIS Data" import, drapes onto
  plss_sections.shp       the topography; sections carry open-ground status
  topo_dem.asc(+.prj)     Arc/Info ASCII elevation grid (AWS Terrain Tiles /
                          Mapzen terrarium, z12 ~28 m here) -> Leapfrog
                          "Elevation Grid" import -> New Topography
  <aoi>.omf               single-file OMF v0.9 bundle: mine/target/claim
                          point sets (grades attached as scalar data), fault
                          polylines draped at DEM elevation, and a decimated
                          topo mesh -> Leapfrog project menu OMF > Import
  README-LEAPFROG.md      exact click-path import instructions + provenance

Run:
  python3 pipelines/leapfrog_export.py --aoi cassia
  python3 pipelines/leapfrog_export.py --aoi cassia --bbox -113.9 42.0 -113.3 42.5 --cell 30
  python3 pipelines/leapfrog_export.py --aoi clearlake --no-omf

The AOI bbox defaults to the union of the AOI geology, PLSS and claims
bundles, padded 0.02 deg. --bbox W S E N overrides (useful to cut a tight
district-scale kit at fine --cell for actual modeling instead of the whole
county at context resolution).
"""
import argparse
import csv
import io
import json
import math
import os
import struct
import sys
import time
import urllib.request
import uuid
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
SITE = os.path.join(ROOT, 'site')
TERRAIN_CACHE = os.path.join(HERE, 'cache', 'terrain')
UA = {'User-Agent': 'nw-mineral-monitor/1.0 (research pipeline; contact: repo owner)'}
TERRAIN_URL = 'https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png'
TERRAIN_ATTRIB = ('AWS Terrain Tiles (Mapzen terrarium): 3DEP/SRTM/GMTED2010 '
                  'composite, https://registry.opendata.aws/terrain-tiles/')
TODAY = time.strftime('%Y-%m-%d')

# ---------------------------------------------------------------- projection
# WGS84 <-> UTM (Transverse Mercator, Snyder 1987 eqs 8-9..8-25). Checked
# against pyproj to < 1 cm across the 8-state footprint; good far beyond the
# ~10 m positional truth of the source datasets.
_A = 6378137.0
_F = 1 / 298.257223563
_E2 = _F * (2 - _F)
_EP2 = _E2 / (1 - _E2)
_K0 = 0.9996


def utm_zone(lon, lat):
    zone = int((lon + 180) // 6) + 1
    zone = max(1, min(60, zone))
    return zone, lat >= 0


def utm_epsg(zone, north):
    return (32600 if north else 32700) + zone


def _mdist(phi):
    e2, e4, e6 = _E2, _E2 ** 2, _E2 ** 3
    return _A * ((1 - e2 / 4 - 3 * e4 / 64 - 5 * e6 / 256) * phi
                 - (3 * e2 / 8 + 3 * e4 / 32 + 45 * e6 / 1024) * math.sin(2 * phi)
                 + (15 * e4 / 256 + 45 * e6 / 1024) * math.sin(4 * phi)
                 - (35 * e6 / 3072) * math.sin(6 * phi))


def utm_fwd(lon, lat, zone, north):
    lon0 = math.radians(zone * 6 - 183)
    phi, lam = math.radians(lat), math.radians(lon)
    sp, cp, tp = math.sin(phi), math.cos(phi), math.tan(phi)
    n = _A / math.sqrt(1 - _E2 * sp * sp)
    t = tp * tp
    c = _EP2 * cp * cp
    a = (lam - lon0) * cp
    m = _mdist(phi)
    e = _K0 * n * (a + (1 - t + c) * a ** 3 / 6
                   + (5 - 18 * t + t * t + 72 * c - 58 * _EP2) * a ** 5 / 120) + 500000.0
    nn = _K0 * (m + n * tp * (a * a / 2
                              + (5 - t + 9 * c + 4 * c * c) * a ** 4 / 24
                              + (61 - 58 * t + t * t + 600 * c - 330 * _EP2) * a ** 6 / 720))
    if not north:
        nn += 10000000.0
    return e, nn


def utm_inv(e, nn, zone, north):
    lon0 = math.radians(zone * 6 - 183)
    x = e - 500000.0
    y = nn - (0.0 if north else 10000000.0)
    m = y / _K0
    mu = m / (_A * (1 - _E2 / 4 - 3 * _E2 ** 2 / 64 - 5 * _E2 ** 3 / 256))
    e1 = (1 - math.sqrt(1 - _E2)) / (1 + math.sqrt(1 - _E2))
    phi1 = (mu + (3 * e1 / 2 - 27 * e1 ** 3 / 32) * math.sin(2 * mu)
            + (21 * e1 ** 2 / 16 - 55 * e1 ** 4 / 32) * math.sin(4 * mu)
            + (151 * e1 ** 3 / 96) * math.sin(6 * mu)
            + (1097 * e1 ** 4 / 512) * math.sin(8 * mu))
    sp, cp, tp = math.sin(phi1), math.cos(phi1), math.tan(phi1)
    c1 = _EP2 * cp * cp
    t1 = tp * tp
    n1 = _A / math.sqrt(1 - _E2 * sp * sp)
    r1 = _A * (1 - _E2) / (1 - _E2 * sp * sp) ** 1.5
    d = x / (n1 * _K0)
    phi = phi1 - (n1 * tp / r1) * (d * d / 2
                                   - (5 + 3 * t1 + 10 * c1 - 4 * c1 * c1 - 9 * _EP2) * d ** 4 / 24
                                   + (61 + 90 * t1 + 298 * c1 + 45 * t1 * t1
                                      - 252 * _EP2 - 3 * c1 * c1) * d ** 6 / 720)
    lam = lon0 + (d - (1 + 2 * t1 + c1) * d ** 3 / 6
                  + (5 - 2 * c1 + 28 * t1 - 3 * c1 * c1 + 8 * _EP2 + 24 * t1 * t1)
                  * d ** 5 / 120) / cp
    return math.degrees(lam), math.degrees(phi)


def utm_wkt(zone, north):
    cm = zone * 6 - 183
    ns = 'N' if north else 'S'
    fn = 0 if north else 10000000
    return ('PROJCS["WGS_1984_UTM_Zone_%d%s",GEOGCS["GCS_WGS_1984",'
            'DATUM["D_WGS_1984",SPHEROID["WGS_1984",6378137.0,298.257223563]],'
            'PRIMEM["Greenwich",0.0],UNIT["Degree",0.0174532925199433]],'
            'PROJECTION["Transverse_Mercator"],PARAMETER["False_Easting",500000.0],'
            'PARAMETER["False_Northing",%d.0],PARAMETER["Central_Meridian",%d.0],'
            'PARAMETER["Scale_Factor",0.9996],PARAMETER["Latitude_Of_Origin",0.0],'
            'UNIT["Meter",1.0]]' % (zone, ns, fn, cm))


# ------------------------------------------------------------------- terrain
def _png_decode(data):
    """Minimal PNG reader for terrarium tiles (8-bit RGB/RGBA, no interlace).
    Returns (width, height, channels, bytearray of unfiltered pixel rows)."""
    if data[:8] != b'\x89PNG\r\n\x1a\n':
        raise ValueError('not a PNG')
    pos, w, h, ch, idat = 8, 0, 0, 0, []
    while pos < len(data):
        ln, typ = struct.unpack('>I4s', data[pos:pos + 8])
        body = data[pos + 8:pos + 8 + ln]
        pos += 12 + ln
        if typ == b'IHDR':
            w, h, depth, ctype, comp, filt, inter = struct.unpack('>IIBBBBB', body)
            if depth != 8 or ctype not in (2, 6) or inter != 0:
                raise ValueError('unsupported PNG layout (depth=%d ctype=%d inter=%d)'
                                 % (depth, ctype, inter))
            ch = 3 if ctype == 2 else 4
        elif typ == b'IDAT':
            idat.append(body)
        elif typ == b'IEND':
            break
    raw = zlib.decompress(b''.join(idat))
    stride = w * ch
    out = bytearray(h * stride)
    prev = bytearray(stride)
    p = 0
    for row in range(h):
        f = raw[p]
        line = bytearray(raw[p + 1:p + 1 + stride])
        p += 1 + stride
        if f == 1:                       # Sub
            for i in range(ch, stride):
                line[i] = (line[i] + line[i - ch]) & 255
        elif f == 2:                     # Up
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 255
        elif f == 3:                     # Average
            for i in range(stride):
                a = line[i - ch] if i >= ch else 0
                line[i] = (line[i] + ((a + prev[i]) >> 1)) & 255
        elif f == 4:                     # Paeth
            for i in range(stride):
                a = line[i - ch] if i >= ch else 0
                b = prev[i]
                c = prev[i - ch] if i >= ch else 0
                pa, pb, pc = abs(b - c), abs(a - c), abs(a + b - 2 * c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                line[i] = (line[i] + pr) & 255
        elif f != 0:
            raise ValueError('bad PNG filter %d' % f)
        out[row * stride:(row + 1) * stride] = line
        prev = line
    return w, h, ch, out


class Terrain(object):
    """Bilinear sampler over cached AWS terrarium tiles at one zoom level."""

    def __init__(self, zoom, offline=False):
        self.z = zoom
        self.n = 2 ** zoom
        self.tiles = {}
        self.offline = offline
        self.failed = 0
        self.fetched = 0
        os.makedirs(TERRAIN_CACHE, exist_ok=True)

    def _tile(self, tx, ty):
        key = (tx, ty)
        if key in self.tiles:
            return self.tiles[key]
        if not (0 <= tx < self.n and 0 <= ty < self.n):
            self.tiles[key] = None
            return None
        path = os.path.join(TERRAIN_CACHE, str(self.z), str(tx))
        fn = os.path.join(path, '%d.png' % ty)
        data = None
        if os.path.exists(fn):
            with open(fn, 'rb') as fh:
                data = fh.read()
        elif not self.offline:
            url = TERRAIN_URL.format(z=self.z, x=tx, y=ty)
            for attempt in range(3):
                try:
                    req = urllib.request.Request(url, headers=UA)
                    with urllib.request.urlopen(req, timeout=30) as resp:
                        data = resp.read()
                    os.makedirs(path, exist_ok=True)
                    with open(fn, 'wb') as fh:
                        fh.write(data)
                    self.fetched += 1
                    break
                except Exception:
                    data = None
                    time.sleep(1.0 + attempt)
        if data is None:
            self.failed += 1
            self.tiles[key] = None
            return None
        try:
            w, h, ch, px = _png_decode(data)
        except ValueError:
            self.failed += 1
            self.tiles[key] = None
            return None
        self.tiles[key] = (w, h, ch, px)
        return self.tiles[key]

    def _px(self, gx, gy):
        """Elevation of one global pixel (nearest tile pixel)."""
        tx, ty = int(gx) // 256, int(gy) // 256
        t = self._tile(tx, ty)
        if t is None:
            return None
        w, h, ch, px = t
        ix, iy = int(gx) - tx * 256, int(gy) - ty * 256
        o = (iy * w + ix) * ch
        return (px[o] * 256.0 + px[o + 1] + px[o + 2] / 256.0) - 32768.0

    def sample(self, lon, lat):
        """Bilinear elevation in meters, or None where no tile decodes."""
        siny = math.sin(math.radians(max(-85.05112878, min(85.05112878, lat))))
        gx = (lon + 180.0) / 360.0 * self.n * 256.0 - 0.5
        gy = (0.5 - math.log((1 + siny) / (1 - siny)) / (4 * math.pi)) * self.n * 256.0 - 0.5
        x0, y0 = math.floor(gx), math.floor(gy)
        fx, fy = gx - x0, gy - y0
        vals = [self._px(x0, y0), self._px(x0 + 1, y0),
                self._px(x0, y0 + 1), self._px(x0 + 1, y0 + 1)]
        if any(v is None for v in vals):
            good = [v for v in vals if v is not None]
            return good[0] if good else None
        return (vals[0] * (1 - fx) * (1 - fy) + vals[1] * fx * (1 - fy)
                + vals[2] * (1 - fx) * fy + vals[3] * fx * fy)


# ---------------------------------------------------------------- shapefiles
SHP_POINT, SHP_POLYLINE, SHP_POLYGON = 1, 3, 5


def _ring_area(ring):
    s = 0.0
    for i in range(len(ring) - 1):
        s += ring[i][0] * ring[i + 1][1] - ring[i + 1][0] * ring[i][1]
    return s / 2.0


def _orient(ring, clockwise):
    if len(ring) < 4:
        return ring
    a = _ring_area(ring)
    if (a < 0) != clockwise:
        return ring[::-1]
    return ring


def write_shapefile(base, shptype, records, fields, prj_wkt):
    """records: list of (geom, attrs). geom is (x,y) for POINT, else a list of
    parts, each a list of (x,y); polygon outer rings first, holes after.
    fields: [(name<=10, 'C'|'N', width, decimals)]. Skips writing if empty."""
    if not records:
        return 0
    shp = io.BytesIO()
    shx = io.BytesIO()
    minx = miny = float('inf')
    maxx = maxy = float('-inf')
    recs = []
    for idx, (geom, _attrs) in enumerate(records):
        if shptype == SHP_POINT:
            x, y = geom
            content = struct.pack('<idd', SHP_POINT, x, y)
            minx, miny = min(minx, x), min(miny, y)
            maxx, maxy = max(maxx, x), max(maxy, y)
        else:
            parts = []
            for part in geom:
                if shptype == SHP_POLYGON:
                    pts, is_hole = part
                    pts = list(pts)
                    if pts[0] != pts[-1]:
                        pts.append(pts[0])
                    pts = _orient(pts, clockwise=not is_hole)
                else:
                    pts = list(part)
                parts.append(pts)
            allp = [pt for part in parts for pt in part]
            bx = [p[0] for p in allp]
            by = [p[1] for p in allp]
            minx, miny = min(minx, min(bx)), min(miny, min(by))
            maxx, maxy = max(maxx, max(bx)), max(maxy, max(by))
            buf = struct.pack('<idddd', shptype, min(bx), min(by), max(bx), max(by))
            buf += struct.pack('<ii', len(parts), len(allp))
            off = 0
            for part in parts:
                buf += struct.pack('<i', off)
                off += len(part)
            for part in parts:
                for x, y in part:
                    buf += struct.pack('<dd', x, y)
            content = buf
        recs.append(content)
    # headers written after sizes are known
    offset = 50                                        # in 16-bit words
    for i, content in enumerate(recs):
        clen = len(content) // 2
        shx.write(struct.pack('>ii', offset, clen))
        shp.write(struct.pack('>ii', i + 1, clen))
        shp.write(content)
        offset += 4 + clen
    def header(total_words):
        h = struct.pack('>iiiiiii', 9994, 0, 0, 0, 0, 0, total_words)
        h += struct.pack('<ii', 1000, shptype)
        h += struct.pack('<8d', minx, miny, maxx, maxy, 0, 0, 0, 0)
        return h
    with open(base + '.shp', 'wb') as fh:
        fh.write(header(50 + sum(4 + len(c) // 2 for c in recs)))
        fh.write(shp.getvalue())
    with open(base + '.shx', 'wb') as fh:
        fh.write(header(50 + 4 * len(recs)))
        fh.write(shx.getvalue())
    # DBF
    fdefs = []
    for name, typ, width, dec in fields:
        fdefs.append((name[:10], typ, min(width, 254), dec))
    reclen = 1 + sum(f[2] for f in fdefs)
    with open(base + '.dbf', 'wb') as fh:
        now = time.localtime()
        fh.write(struct.pack('<BBBBIHH20x', 3, now.tm_year - 1900, now.tm_mon,
                             now.tm_mday, len(recs), 32 + 32 * len(fdefs) + 1, reclen))
        for name, typ, width, dec in fdefs:
            fh.write(struct.pack('<11sc4xBB14x', name.encode('ascii'),
                                 typ.encode('ascii'), width, dec))
        fh.write(b'\x0d')
        for _geom, attrs in records:
            fh.write(b' ')
            for name, typ, width, dec in fdefs:
                v = attrs.get(name, '')
                if typ == 'N':
                    if v is None or v == '':
                        s = b' ' * width
                    else:
                        fmt = '%%%d.%df' % (width, dec) if dec else '%%%dd' % width
                        try:
                            s = (fmt % (float(v) if dec else int(round(float(v))))).encode('ascii')
                        except (ValueError, TypeError):
                            s = b' ' * width
                        if len(s) > width:
                            s = b'*' * width
                else:
                    s = ('' if v is None else str(v)).encode('latin-1', 'replace')[:width]
                    s = s.ljust(width)
                fh.write(s[:width])
        fh.write(b'\x1a')
    with open(base + '.prj', 'w') as fh:
        fh.write(prj_wkt)
    return len(recs)


# ----------------------------------------------------------------- OMF v0.9
class OmfWriter(object):
    """Writes Open Mining Format v0.9 files byte-compatible with the
    reference `omf` 1.0.1 Python package (and thus Leapfrog's OMF import:
    Leapfrog Geo menu > OMF > Import). Binary arrays are zlib-compressed
    little-endian float64/int64 exactly as the reference serializer emits."""

    def __init__(self, name, description, revision=''):
        self.buf = io.BytesIO()
        self.buf.write(b'\x00' * 60)
        self.reg = {}
        self.now = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
        self.elements = []
        self.project = {'name': name, 'description': description,
                        'author': 'nw-mineral-monitor leapfrog_export',
                        'revision': revision, 'units': 'm',
                        'origin': [0.0, 0.0, 0.0]}

    def _add(self, cls, props):
        uid = str(uuid.uuid4())
        d = {'date_created': self.now, 'date_modified': self.now, '__class__': cls}
        d.update(props)
        self.reg[uid] = d
        return uid

    def _blob(self, fmt_char, flat):
        start = self.buf.tell()
        comp = zlib.compressobj()
        out = bytearray()
        chunk = 65536
        for i in range(0, len(flat), chunk):
            out += comp.compress(struct.pack('<%d%s' % (len(flat[i:i + chunk]), fmt_char),
                                             *flat[i:i + chunk]))
        out += comp.flush()
        self.buf.write(out)
        return {'start': start, 'dtype': '<f8' if fmt_char == 'd' else '<i8',
                'length': self.buf.tell() - start}

    def _f8(self, cls, flat):
        return self._add(cls, {'array': self._blob('d', [float(v) for v in flat])})

    def _i8(self, cls, flat):
        return self._add(cls, {'array': self._blob('q', [int(v) for v in flat])})

    def scalar_data(self, name, values, location='vertices', description=''):
        def num(v):
            try:
                return float(v)
            except (TypeError, ValueError):
                return float('nan')   # prose notes in a numeric column -> no-data
        arr = self._f8('ScalarArray', [num(v) for v in values])
        return self._add('ScalarData', {'name': name, 'description': description,
                                        'location': location, 'array': arr})

    def string_data(self, name, values, location='vertices', description=''):
        arr = self._add('StringArray', {'array': ['' if v is None else str(v)
                                                  for v in values]})
        return self._add('StringData', {'name': name, 'description': description,
                                        'location': location, 'array': arr})

    def pointset(self, name, description, xyz, color, data_uids=()):
        flat = [c for pt in xyz for c in pt]
        verts = self._f8('Vector3Array', flat)
        geom = self._add('PointSetGeometry',
                         {'origin': [0.0, 0.0, 0.0], 'vertices': verts})
        el = self._add('PointSetElement',
                       {'name': name, 'description': description,
                        'data': list(data_uids), 'color': list(color),
                        'textures': [], 'subtype': 'point', 'geometry': geom})
        self.elements.append(el)
        return el

    def lineset(self, name, description, xyz, segments, color, data_uids=()):
        verts = self._f8('Vector3Array', [c for pt in xyz for c in pt])
        segs = self._i8('Int2Array', [i for s in segments for i in s])
        geom = self._add('LineSetGeometry',
                         {'origin': [0.0, 0.0, 0.0], 'vertices': verts,
                          'segments': segs})
        el = self._add('LineSetElement',
                       {'name': name, 'description': description,
                        'data': list(data_uids), 'color': list(color),
                        'subtype': 'line', 'geometry': geom})
        self.elements.append(el)
        return el

    def surface(self, name, description, xyz, triangles, color, data_uids=()):
        verts = self._f8('Vector3Array', [c for pt in xyz for c in pt])
        tris = self._i8('Int3Array', [i for t in triangles for i in t])
        geom = self._add('SurfaceGeometry',
                         {'origin': [0.0, 0.0, 0.0], 'vertices': verts,
                          'triangles': tris})
        el = self._add('SurfaceElement',
                       {'name': name, 'description': description,
                        'data': list(data_uids), 'color': list(color),
                        'textures': [], 'subtype': 'surface', 'geometry': geom})
        self.elements.append(el)
        return el

    def write(self, fname):
        self.project['elements'] = self.elements
        puid = str(uuid.uuid4())
        d = {'date_created': self.now, 'date_modified': self.now,
             '__class__': 'Project'}
        d.update(self.project)
        self.reg[puid] = d
        json_start = self.buf.tell()
        self.buf.write(json.dumps(self.reg).encode('utf-8'))
        self.buf.seek(0)
        self.buf.write(b'\x84\x83\x82\x81')
        self.buf.write(struct.pack('<32s', b'OMF-v0.9.0'.ljust(32, b'\x00')))
        self.buf.write(struct.pack('<16s', uuid.UUID(puid).bytes))
        self.buf.write(struct.pack('<Q', json_start))
        with open(fname, 'wb') as fh:
            fh.write(self.buf.getvalue())


# -------------------------------------------------------------- data loading
def load_json(path):
    if not os.path.exists(path):
        return None
    with open(path) as fh:
        return json.load(fh)


def poly_bbox(coords, box):
    for part in coords:
        for ring in part:
            for x, y in ring:
                box[0] = min(box[0], x)
                box[1] = min(box[1], y)
                box[2] = max(box[2], x)
                box[3] = max(box[3], y)


def geom_parts(geometry):
    """GeoJSON Polygon/MultiPolygon -> [(ring, is_hole)], outer first per poly."""
    if geometry is None:
        return []
    t = geometry.get('type')
    if t == 'Polygon':
        return [(list(map(tuple, r)), i > 0)
                for i, r in enumerate(geometry['coordinates'])]
    if t == 'MultiPolygon':
        return [(list(map(tuple, r)), i > 0)
                for poly in geometry['coordinates'] for i, r in enumerate(poly)]
    return []


# --------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description='Export an AOI as a Leapfrog Geo starter kit')
    ap.add_argument('--aoi', default='cassia', help='AOI key (matches site/data/*/<aoi>.json)')
    ap.add_argument('--out', default=None, help='output dir (default exports/leapfrog/<aoi>)')
    ap.add_argument('--bbox', nargs=4, type=float, metavar=('W', 'S', 'E', 'N'),
                    help='override AOI bbox (lon/lat degrees)')
    ap.add_argument('--cell', type=float, default=None,
                    help='DEM cell size m (default: auto, <=1.6M nodes)')
    ap.add_argument('--zoom', type=int, default=12, help='terrain tile zoom (default 12, ~28 m)')
    ap.add_argument('--no-dem', action='store_true', help='skip the Arc/Info ASCII grid')
    ap.add_argument('--no-omf', action='store_true', help='skip the OMF bundle')
    ap.add_argument('--offline', action='store_true',
                    help='never fetch terrain tiles (cache only; missing -> elev 0)')
    args = ap.parse_args()

    aoi = args.aoi
    out = args.out or os.path.join(ROOT, 'exports', 'leapfrog', aoi)
    os.makedirs(out, exist_ok=True)
    d = os.path.join(SITE, 'data')

    grades = load_json(os.path.join(d, 'grades', 'grades.json'))
    targets = load_json(os.path.join(d, 'targets', '%s.json' % aoi))
    geology = load_json(os.path.join(d, 'geology', '%s.json' % aoi))
    claims = load_json(os.path.join(d, 'openground', '%s_claims.json' % aoi))
    og = load_json(os.path.join(d, 'openground', '%s.json' % aoi))
    plss = load_json(os.path.join(d, 'plss', '%s.json' % aoi))
    missing = [n for n, v in [('grades', grades), ('targets', targets), ('geology', geology),
                              ('claims', claims), ('openground', og), ('plss', plss)] if v is None]
    if missing:
        print('note: no %s bundle for %s — those layers are skipped' % ('/'.join(missing), aoi))

    # ---- AOI bbox
    if args.bbox:
        box = list(args.bbox)
    else:
        box = [float('inf'), float('inf'), float('-inf'), float('-inf')]
        if geology:
            for u in geology.get('units', []):
                if u.get('g'):
                    poly_bbox(u['g'], box)
        if plss:
            for f in plss.get('features', []):
                for ring, _hole in geom_parts(f.get('geometry')):
                    for x, y in ring:
                        box[0], box[1] = min(box[0], x), min(box[1], y)
                        box[2], box[3] = max(box[2], x), max(box[3], y)
        if claims:
            for c in claims.get('active', []) + claims.get('closed', []):
                if c.get('x') is not None:
                    box[0], box[1] = min(box[0], c['x']), min(box[1], c['y'])
                    box[2], box[3] = max(box[2], c['x']), max(box[3], c['y'])
        if box[0] > box[2]:
            sys.exit('no geometry found to derive a bbox — pass --bbox W S E N')
        box = [box[0] - 0.02, box[1] - 0.02, box[2] + 0.02, box[3] + 0.02]
    w, s, e, n = box
    clon, clat = (w + e) / 2, (s + n) / 2
    zone, north = utm_zone(clon, clat)
    epsg = utm_epsg(zone, north)
    wkt = utm_wkt(zone, north)
    fwd = lambda lon, lat: utm_fwd(lon, lat, zone, north)
    print('AOI %s  bbox %.4f %.4f %.4f %.4f  ->  WGS84 / UTM %d%s (EPSG:%d)'
          % (aoi, w, s, e, n, zone, 'N' if north else 'S', epsg))

    terr = Terrain(args.zoom, offline=args.offline)
    probe = terr.sample(clon, clat)
    have_elev = probe is not None
    if not have_elev:
        print('WARNING: terrain tiles unreachable and not cached — elevations '
              'will be 0 and the DEM grid is skipped. Re-run online for real Z.')

    def elev(lon, lat):
        if not have_elev:
            return 0.0
        v = terr.sample(lon, lat)
        return 0.0 if v is None else v

    inb = lambda x, y: (w <= x <= e) and (s <= y <= n)
    summary = []

    # ---- graded mines (columnar national table -> AOI rows)
    mines = []
    if grades:
        cols = ['name', 'st', 'cnty', 'dist', 'x', 'y', 'au', 'ag', 'pb', 'zn', 'cu', 'sb',
                'wo3', 'hgf', 'usd', 'ton', 'yd3', 'plc', 'open', 'com', 'src', 'url']
        cols = [c for c in cols if c in grades]
        for i in range(grades['n']):
            x, y = grades['x'][i], grades['y'][i]
            if x is None or y is None or not inb(x, y):
                continue
            mines.append({c: grades[c][i] for c in cols})
    if mines:
        path = os.path.join(out, 'mines_grades.csv')
        with open(path, 'w', newline='') as fh:
            cw = csv.writer(fh)
            head = ['east', 'north', 'elev_m', 'name', 'state', 'county', 'district',
                    'au_ozt', 'ag_ozt', 'pb_pct', 'zn_pct', 'cu_pct', 'sb_pct',
                    'wo3_units', 'hg_flasks', 'usd_per_ton', 'tons', 'usd_per_yd3',
                    'placer', 'workings_open_m', 'commodities', 'source', 'url']
            cw.writerow(head)
            for m in mines:
                ee, nn = fwd(m['x'], m['y'])
                zz = elev(m['x'], m['y'])
                cw.writerow(['%.2f' % ee, '%.2f' % nn, '%.1f' % zz,
                             m.get('name'), m.get('st'), m.get('cnty'), m.get('dist'),
                             m.get('au'), m.get('ag'), m.get('pb'), m.get('zn'),
                             m.get('cu'), m.get('sb'), m.get('wo3'), m.get('hgf'),
                             m.get('usd'), m.get('ton'), m.get('yd3'),
                             m.get('plc'), (m.get('open') if (m.get('open') or 0) > 0 else ''),
                             m.get('com'), m.get('src'), m.get('url')])
        summary.append('mines_grades.csv — %d graded mines (grade columns preserve '
                       'units: oz/ton, %%, WO3 units, Hg flasks)' % len(mines))

    # ---- WS6 targets
    tg = (targets or {}).get('targets', [])
    tg = [t for t in tg if t.get('cx') is not None and inb(t['cx'], t['cy'])]
    if tg:
        path = os.path.join(out, 'targets.csv')
        with open(path, 'w', newline='') as fh:
            cw = csv.writer(fh)
            cw.writerow(['east', 'north', 'elev_m', 'tier', 'score', 'unit', 'age',
                         'area_km2', 'money', 'tier_name'])
            for t in tg:
                ee, nn = fwd(t['cx'], t['cy'])
                cw.writerow(['%.2f' % ee, '%.2f' % nn, '%.1f' % elev(t['cx'], t['cy']),
                             t.get('tier'), t.get('score'), t.get('nm'), t.get('age'),
                             t.get('area_km2'), 1 if t.get('money') else 0,
                             t.get('tierName')])
        summary.append('targets.csv — %d scored geology targets (unit centroids)' % len(tg))

    # ---- claims (points)
    def claim_rows(rows, status):
        outrows = []
        for c in rows or []:
            if c.get('x') is None or not inb(c['x'], c['y']):
                continue
            ee, nn = fwd(c['x'], c['y'])
            outrows.append((c, ee, nn, elev(c['x'], c['y']), status))
        return outrows

    cl_a = claim_rows((claims or {}).get('active'), 'ACTIVE')
    cl_c = claim_rows((claims or {}).get('closed'), 'CLOSED')
    for rows, tag in ((cl_a, 'active'), (cl_c, 'closed')):
        if not rows:
            continue
        path = os.path.join(out, 'claims_%s.csv' % tag)
        with open(path, 'w', newline='') as fh:
            cw = csv.writer(fh)
            cw.writerow(['east', 'north', 'elev_m', 'serial', 'name', 'type',
                         'disposition', 'acres', 'status'])
            for c, ee, nn, zz, st in rows:
                cw.writerow(['%.2f' % ee, '%.2f' % nn, '%.1f' % zz, c.get('ser'),
                             c.get('name'), c.get('type'), c.get('disp'),
                             c.get('acres'), st])
        fields = [('serial', 'C', 16, 0), ('name', 'C', 60, 0), ('type', 'C', 4, 0),
                  ('disp', 'C', 24, 0), ('acres', 'N', 12, 2), ('status', 'C', 8, 0)]
        write_shapefile(os.path.join(out, 'claims_%s' % tag), SHP_POINT,
                        [((ee, nn), {'serial': c.get('ser'), 'name': c.get('name'),
                                     'type': c.get('type'), 'disp': c.get('disp'),
                                     'acres': c.get('acres'), 'status': st})
                         for c, ee, nn, zz, st in rows], fields, wkt)
        summary.append('claims_%s.csv/.shp — %d BLM claim centroids (centroids, NOT '
                       'boundaries — BLM public GIS carries no corners)' % (tag, len(rows)))

    # ---- geology unit polygons
    units = [u for u in (geology or {}).get('units', []) if u.get('g')]
    if units:
        recs = []
        for u in units:
            parts = []
            for poly in u['g']:
                for ri, ring in enumerate(poly):
                    parts.append(([fwd(x, y) for x, y in ring], ri > 0))
            recs.append((parts, {'id': u.get('id'), 'name': (u.get('nm') or '')[:120],
                                 'age': u.get('age'), 'lith': (u.get('li') or '')[:200],
                                 'desc': (u.get('de') or '')[:254], 'src': u.get('src'),
                                 't0_ma': u.get('t0'), 't1_ma': u.get('t1')}))
        nrec = write_shapefile(os.path.join(out, 'geology_units'), SHP_POLYGON, recs,
                               [('id', 'C', 12, 0), ('name', 'C', 120, 0), ('age', 'C', 40, 0),
                                ('lith', 'C', 200, 0), ('desc', 'C', 254, 0),
                                ('src', 'C', 12, 0), ('t0_ma', 'N', 10, 2),
                                ('t1_ma', 'N', 10, 2)], wkt)
        summary.append('geology_units.shp — %d harmonized map polygons '
                       '(import as GIS vector data; drape onto topography)' % nrec)

    # ---- faults
    faults = [f for f in (geology or {}).get('faults', []) if f.get('path')]
    if faults:
        recs = [([[fwd(x, y) for x, y in f['path']]],
                 {'name': f.get('nm'), 'type': f.get('ty'), 'src': f.get('src')})
                for f in faults]
        nrec = write_shapefile(os.path.join(out, 'faults'), SHP_POLYLINE, recs,
                               [('name', 'C', 80, 0), ('type', 'C', 24, 0),
                                ('src', 'C', 24, 0)], wkt)
        summary.append('faults.shp — %d mapped structures' % nrec)

    # ---- PLSS sections + open-ground status
    if plss:
        status = {}
        for sec in (og or {}).get('sections', []):
            status[sec.get('id')] = sec
        recs = []
        for f in plss.get('features', []):
            rings = geom_parts(f.get('geometry'))
            if not rings:
                continue
            p = f.get('properties', {})
            sid = p.get('id')
            st = status.get(sid, {})
            recs.append(([([fwd(x, y) for x, y in ring], hole) for ring, hole in rings],
                         {'id': sid, 'label': p.get('lab') or st.get('lab'),
                          'status': st.get('st'), 'agency': st.get('ag'),
                          'n_active': st.get('nA'), 'n_closed': st.get('nC')}))
        nrec = write_shapefile(os.path.join(out, 'plss_sections'), SHP_POLYGON, recs,
                               [('id', 'C', 24, 0), ('label', 'C', 24, 0),
                                ('status', 'C', 12, 0), ('agency', 'C', 8, 0),
                                ('n_active', 'N', 8, 0), ('n_closed', 'N', 8, 0)], wkt)
        summary.append('plss_sections.shp — %d sections with open-ground status '
                       '(OPEN/ACTIVE/CLOSED_ONLY/WITHDRAWN/NONFEDERAL/QUIET; research '
                       'lead only, never a title opinion)' % nrec)

    # ---- DEM (Arc/Info ASCII grid) + shared UTM grid math
    corners = [fwd(w, s), fwd(e, s), fwd(w, n), fwd(e, n),
               fwd(clon, s), fwd(clon, n), fwd(w, clat), fwd(e, clat)]
    ge = [min(c[0] for c in corners), min(c[1] for c in corners),
          max(c[0] for c in corners), max(c[1] for c in corners)]

    def sample_grid(cell):
        ncols = int(math.ceil((ge[2] - ge[0]) / cell))
        nrows = int(math.ceil((ge[3] - ge[1]) / cell))
        rows = []
        for r in range(nrows):                     # top -> bottom
            yy = ge[3] - (r + 0.5) * cell
            row = []
            for cix in range(ncols):
                xx = ge[0] + (cix + 0.5) * cell
                lon, lat = utm_inv(xx, yy, zone, north)
                v = terr.sample(lon, lat)
                row.append(v)
            rows.append(row)
        return ncols, nrows, rows

    if have_elev and not args.no_dem:
        cell = args.cell
        if not cell:
            area = (ge[2] - ge[0]) * (ge[3] - ge[1])
            cell = max(20.0, round(math.sqrt(area / 1.6e6) / 5) * 5)
        ncols = int(math.ceil((ge[2] - ge[0]) / cell))
        nrows = int(math.ceil((ge[3] - ge[1]) / cell))
        print('DEM grid %d x %d @ %.0f m (zoom %d terrarium) ...' % (ncols, nrows, cell, args.zoom))
        _, _, rows = sample_grid(cell)
        path = os.path.join(out, 'topo_dem.asc')
        with open(path, 'w') as fh:
            fh.write('ncols %d\nnrows %d\nxllcorner %.3f\nyllcorner %.3f\n'
                     'cellsize %.3f\nNODATA_value -9999\n'
                     % (ncols, nrows, ge[0], ge[3] - nrows * cell, cell))
            for row in rows:
                fh.write(' '.join('-9999' if v is None else '%.1f' % v for v in row))
                fh.write('\n')
        with open(os.path.join(out, 'topo_dem.prj'), 'w') as fh:
            fh.write(wkt)
        summary.append('topo_dem.asc — %d x %d Arc/Info ASCII grid @ %.0f m (%s)'
                       % (ncols, nrows, cell, TERRAIN_ATTRIB.split(':')[0]))

    # ---- OMF bundle
    if not args.no_omf:
        omfw = OmfWriter('%s — NW Mineral Monitor' % aoi,
                         'AOI research bundle exported %s. CRS: WGS84 / UTM zone '
                         '%d%s (EPSG:%d), meters. Elevations: %s.'
                         % (TODAY, zone, 'N' if north else 'S', epsg,
                            TERRAIN_ATTRIB if have_elev else 'UNAVAILABLE (all 0)'),
                         revision=TODAY)
        if mines:
            xyz = []
            for m in mines:
                ee, nn = fwd(m['x'], m['y'])
                xyz.append((ee, nn, elev(m['x'], m['y'])))
            data = [omfw.string_data('name', [m.get('name') for m in mines]),
                    omfw.string_data('district', [m.get('dist') for m in mines]),
                    omfw.string_data('commodities', [m.get('com') for m in mines]),
                    omfw.string_data('source', [m.get('src') for m in mines])]
            for key, label in (('au', 'Au oz/ton'), ('ag', 'Ag oz/ton'), ('pb', 'Pb %'),
                               ('zn', 'Zn %'), ('cu', 'Cu %'), ('sb', 'Sb %'),
                               ('usd', 'USD/ton (historic)'), ('ton', 'tons'),
                               ('open', 'workings open m')):
                vals = [m.get(key) for m in mines]
                if any(v is not None and v not in ('', -1) for v in vals):
                    vals = [None if (v is None or v == -1) else v for v in vals]
                    data.append(omfw.scalar_data(label, vals))
            omfw.pointset('Mines (graded)', 'best cited grade per mine; see '
                          'mines_grades.csv for full provenance', xyz, (201, 133, 0), data)
        if tg:
            xyz = [(lambda p: (p[0], p[1], elev(t['cx'], t['cy'])))(fwd(t['cx'], t['cy']))
                   for t in tg]
            data = [omfw.scalar_data('score', [t.get('score') for t in tg]),
                    omfw.scalar_data('tier', [t.get('tier') for t in tg]),
                    omfw.string_data('unit', [t.get('nm') for t in tg])]
            omfw.pointset('Targets (WS6 scored)', 'geology-target unit centroids',
                          xyz, (45, 212, 191), data)
        for rows, label, color in ((cl_a, 'Claims active', (57, 135, 229)),
                                   (cl_c, 'Claims closed', (110, 110, 110))):
            if rows:
                xyz = [(ee, nn, zz) for _c, ee, nn, zz, _s in rows]
                data = [omfw.string_data('serial', [c.get('ser') for c, *_ in rows]),
                        omfw.string_data('claim', [c.get('name') for c, *_ in rows])]
                omfw.pointset(label, 'BLM MLRS claim centroids (not boundaries)',
                              xyz, color, data)
        if faults:
            verts, segs = [], []
            for f in faults:
                base = len(verts)
                for x, y in f['path']:
                    ee, nn = fwd(x, y)
                    verts.append((ee, nn, elev(x, y) + 2.0))
                segs.extend((base + i, base + i + 1) for i in range(len(f['path']) - 1))
            if segs:
                omfw.lineset('Faults (draped)', 'mapped structures at DEM elevation +2 m',
                             verts, segs, (212, 165, 63))
        if have_elev:
            # decimated context topo mesh (the .asc carries the fine grid)
            area = (ge[2] - ge[0]) * (ge[3] - ge[1])
            ocell = max(60.0, round(math.sqrt(area / 1.2e5) / 5) * 5)
            ncols, nrows, rows = sample_grid(ocell)
            xyz, idx = [], {}
            for r in range(nrows):
                for cix in range(ncols):
                    v = rows[r][cix]
                    idx[(r, cix)] = len(xyz)
                    xyz.append((ge[0] + (cix + 0.5) * ocell, ge[3] - (r + 0.5) * ocell,
                                0.0 if v is None else v))
            tris = []
            for r in range(nrows - 1):
                for cix in range(ncols - 1):
                    a, b = idx[(r, cix)], idx[(r, cix + 1)]
                    c, dd = idx[(r + 1, cix)], idx[(r + 1, cix + 1)]
                    tris.append((a, c, b))
                    tris.append((b, c, dd))
            omfw.surface('Topography (context, %.0f m)' % ocell,
                         'decimated terrarium mesh — import topo_dem.asc for the '
                         'modeling-grade grid', xyz, tris, (120, 120, 120))
        omf_path = os.path.join(out, '%s.omf' % aoi)
        omfw.write(omf_path)
        summary.append('%s.omf — OMF v0.9 bundle (Leapfrog: Leapfrog Geo menu > OMF > Import)'
                       % aoi)

    # ---- README
    with open(os.path.join(out, 'README-LEAPFROG.md'), 'w') as fh:
        fh.write(README_TMPL.format(
            aoi=aoi, date=TODAY, zone='%d%s' % (zone, 'N' if north else 'S'), epsg=epsg,
            bbox='%.4f %.4f %.4f %.4f' % (w, s, e, n),
            elev=('%s, sampled at tile zoom %d (~%.0f m/px here)'
                  % (TERRAIN_ATTRIB, args.zoom,
                     156543.03 / (2 ** args.zoom) * math.cos(math.radians(clat)))
                  if have_elev else
                  'UNAVAILABLE at export time — every elevation in this kit is 0; '
                  're-run pipelines/leapfrog_export.py online before modeling'),
            files='\n'.join('- ' + s for s in summary)))

    print('\nwrote %s:' % out)
    for s in summary:
        print('  ' + s)
    if terr.failed:
        print('note: %d terrain tiles failed to decode/fetch — those cells are NODATA'
              % terr.failed)


README_TMPL = """# Leapfrog Geo starter kit — {aoi}

Exported {date} by `pipelines/leapfrog_export.py`. Everything below is in
**WGS84 / UTM zone {zone} (EPSG:{epsg}), meters** — set exactly that CRS when
Leapfrog asks, and every layer lands in the same XYZ space.
AOI bbox (lon/lat): {bbox}
Elevations: {elev}

## Files

{files}

## Import order in Leapfrog Geo

1. **Topography** — right-click **Topographies > New Topography > Import
   Elevation Grid**, pick `topo_dem.asc` (Arc/Info ASCII grid). This becomes
   the project topography every GIS layer drapes onto.
2. **GIS vectors** — right-click **GIS Data, Maps and Photos > Import Vector
   Data**, multi-select `geology_units.shp`, `faults.shp`,
   `plss_sections.shp`, `claims_active.shp`, `claims_closed.shp`. When asked
   for elevation handling choose *Drape on topography* (the attributes ride
   along; colour `plss_sections` by `status`, claims by `status`).
3. **Points** — right-click **Points > Import Points**, pick
   `mines_grades.csv`: East/North/Elev are the first three columns; keep the
   grade columns as numeric data so you can filter/colour by `au_ozt`,
   `ag_ozt`, etc. Repeat for `targets.csv` (colour by `score`).
4. **Or do 1–3 in one step** — **Leapfrog Geo menu > OMF > Import** on
   `{aoi}.omf` brings in the point sets (with grades attached), draped
   faults, and a context topo mesh. OMF import is one-shot (objects cannot
   be reloaded), so prefer the CSV/SHP/ASC route for layers you expect to
   refresh from the monitor.

## Honesty notes (same rules as the map)

- Claim locations are **BLM MLRS centroids**, not staked corners; a claim
  point in 3-D space still cannot establish title. Verify serials at
  mlrs.blm.gov before acting on open ground.
- Section `status` is the monitor's conservative research lead
  (OPEN / ACTIVE / CLOSED_ONLY / WITHDRAWN / NONFEDERAL / QUIET), not a
  mineral-title opinion.
- Grades are the best *cited historic* figure per mine with source text
  preserved in `mines_grades.csv` — they are leads for sampling, not a
  resource estimate. Units differ by column (oz/ton, %, WO3 units, Hg
  flasks, $/yd³) and are never converted.
- Terrain is a public composite (3DEP/SRTM); expect ~10 m vertical noise —
  fine for draping and viewshed thinking, not for survey work.
"""


if __name__ == '__main__':
    main()
