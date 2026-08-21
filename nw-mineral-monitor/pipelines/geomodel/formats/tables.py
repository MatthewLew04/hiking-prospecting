"""CSV tables: points, drillholes (collar / survey / intervals), planar
structural data and block models -- in the column conventions Leapfrog's
importers expect.  Stdlib only (``csv`` module).

Column matching is by header synonym, case-insensitive, ignoring every
character that is not a letter or digit (so ``Hole_ID``, ``holeid`` and
``HOLE ID`` are the same).  The delimiter is sniffed (comma, semicolon, tab,
pipe) with a comma fallback.  Numeric columns become floats (blank -> None),
anything else stays text.

Readers accept a path / bytes / file object; writers a path or a binary file
object and return the path written (bytes for a BytesIO).  Anything unusual
is recorded in ``obj.metadata['warnings']``.
"""
import csv
import io
import math
import os
import re

from ..model import PointSet, BlockModel, Drillholes, farray, NAN

FORMAT_ID = 'csv'

# ----------------------------------------------------------- synonym tables
X_SYN = ('x', 'east', 'easting', 'xcoord', 'coordx', 'xcollar', 'collarx', 'xutm', 'utmx', 'utme',
         'xm', 'eastm', 'lon', 'longitude', 'e')
Y_SYN = ('y', 'north', 'northing', 'ycoord', 'coordy', 'ycollar', 'collary', 'yutm', 'utmy', 'utmn',
         'ym', 'northm', 'lat', 'latitude', 'n')
Z_SYN = ('z', 'elev', 'elevation', 'rl', 'alt', 'altitude', 'zcoord', 'coordz', 'zcollar', 'collarz',
         'zm', 'elevm', 'height')
HOLE_SYN = ('holeid', 'hole', 'bhid', 'dhid', 'ddh', 'drillhole', 'hole_no', 'holeno', 'holename',
            'id', 'name', 'well', 'wellid')
DEPTH_SYN = ('depth', 'maxdepth', 'eoh', 'totaldepth', 'td', 'length', 'finaldepth', 'enddepth',
             'holedepth', 'depthm')
SVY_DEPTH_SYN = ('depth', 'at', 'distance', 'md', 'dist', 'surveydepth', 'measureddepth', 'station')
AZI_SYN = ('azimuth', 'azi', 'az', 'bearing', 'brg', 'azim', 'trend', 'direction')
DIP_SYN = ('dip', 'inclination', 'incl', 'inc', 'plunge', 'dipangle')
FROM_SYN = ('from', 'depthfrom', 'start', 'fromm', 'fromdepth', 'top', 'startdepth', 'depfrom')
TO_SYN = ('to', 'depthto', 'end', 'tom', 'todepth', 'bottom', 'enddepth', 'depto')
DIPAZ_SYN = ('dipazimuth', 'dipdir', 'dipdirection', 'dipazi', 'azimuth', 'azi', 'dipaz', 'ddr')
STRIKE_SYN = ('strike', 'strikeazimuth', 'strikedir')
POLARITY_SYN = ('polarity', 'pol', 'younging', 'facing', 'overturned')
BM_X_SYN = ('x', 'xc', 'xcentre', 'xcenter', 'centroidx', 'xcentroid', 'centrex', 'centerx', 'xworld',
            'east', 'easting', 'xmid', 'midx')
BM_Y_SYN = ('y', 'yc', 'ycentre', 'ycenter', 'centroidy', 'ycentroid', 'centrey', 'centery', 'yworld',
            'north', 'northing', 'ymid', 'midy')
BM_Z_SYN = ('z', 'zc', 'zcentre', 'zcenter', 'centroidz', 'zcentroid', 'centrez', 'centerz', 'zworld',
            'elev', 'elevation', 'rl', 'zmid', 'midz')
DX_SYN = ('dx', 'xinc', 'xsize', 'sizex', 'xlength', 'xdim', 'dimx', 'blocksizex', 'xblocksize', 'lengthx', 'xlen')
DY_SYN = ('dy', 'yinc', 'ysize', 'sizey', 'ylength', 'ydim', 'dimy', 'blocksizey', 'yblocksize', 'lengthy', 'ylen')
DZ_SYN = ('dz', 'zinc', 'zsize', 'sizez', 'zlength', 'zdim', 'dimz', 'blocksizez', 'zblocksize', 'lengthz', 'zlen')


# ------------------------------------------------------------------ helpers
def _load_bytes(src):
    if isinstance(src, (bytes, bytearray, memoryview)):
        return bytes(src)
    if hasattr(src, 'read'):
        data = src.read()
        return data if isinstance(data, bytes) else data.encode('utf-8')
    with open(src, 'rb') as fh:
        return fh.read()


def _src_path(src):
    return os.fspath(src) if isinstance(src, (str, os.PathLike)) else None


def _emit(dst, data):
    if hasattr(dst, 'write'):
        dst.write(data)
        if isinstance(dst, io.BytesIO):
            return dst.getvalue()
        return getattr(dst, 'name', None)
    with open(dst, 'wb') as fh:
        fh.write(data)
    return os.fspath(dst)


def _decode(data):
    if data.startswith(b'\xef\xbb\xbf'):
        data = data[3:]
    for enc in ('utf-8', 'cp1252', 'latin-1'):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode('latin-1', 'replace')


def norm(name):
    """Header normalisation: lower-case, letters and digits only."""
    return re.sub(r'[^a-z0-9]', '', str(name or '').lower())


def _stem(path, default):
    return os.path.splitext(os.path.basename(path))[0] if path else default


def _num(s):
    """Float from a cell; None when blank / not numeric."""
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    t = str(s).strip()
    if not t or t.lower() in ('na', 'n/a', 'nan', 'null', 'none', '-', '#n/a'):
        return None
    try:
        return float(t)
    except ValueError:
        t2 = t.replace(',', '')
        try:
            return float(t2)
        except ValueError:
            return None


def _is_numeric_column(values):
    seen = False
    for v in values:
        t = str(v).strip() if v is not None else ''
        if not t:
            continue
        if _num(t) is None:
            return False
        seen = True
    return seen


def _fmt(v):
    if v is None:
        return ''
    if isinstance(v, float):
        if v != v or v in (math.inf, -math.inf):
            return ''
        r = repr(v)
        return r[:-2] if r.endswith('.0') else r
    return str(v)


def sniff_dialect(text):
    """csv dialect from a text sample: Sniffer over , ; tab | with a comma
    fallback (and a whitespace fallback for space-separated tables)."""
    sample = text[:8192]
    lines = [ln for ln in sample.splitlines() if ln.strip()]
    first = lines[0] if lines else ''
    try:
        d = csv.Sniffer().sniff(sample, delimiters=',;\t|')
        if d.delimiter in ',;\t|' and len(first.split(d.delimiter)) >= 2:
            return d
    except csv.Error:
        pass
    counts = {dl: first.count(dl) for dl in ',;\t|'}
    best = max(counts, key=counts.get)
    if counts[best] > 0:
        class _D(csv.excel):
            delimiter = best
        return _D
    if len(first.split()) >= 2:
        class _W(csv.excel):
            delimiter = ' '
            skipinitialspace = True
        return _W
    return csv.excel


class Table(object):
    """Parsed CSV: ``headers`` (original), ``columns`` {header: list of
    cells (str)}, ``rows`` count, ``preamble`` (leading '#'/'key: value'
    lines) and ``warnings``."""

    def __init__(self, headers, columns, preamble, warnings, dialect):
        self.headers = headers
        self.columns = columns
        self.n = len(columns[headers[0]]) if headers else 0
        self.preamble = preamble
        self.warnings = warnings
        self.delimiter = getattr(dialect, 'delimiter', ',')

    def find(self, synonyms, explicit=None, exclude=()):
        """Header matching ``explicit`` (exact, then normalised) or the
        first synonym (synonym priority order) -- or None."""
        if explicit is not None:
            if explicit in self.columns:
                return explicit
            for h in self.headers:
                if norm(h) == norm(explicit):
                    return h
            raise KeyError('column %r not in %s' % (explicit, self.headers))
        normed = [(norm(h), h) for h in self.headers if h not in exclude]
        for s in synonyms:
            ns = norm(s)
            for nh, h in normed:
                if nh == ns:
                    return h
        return None

    def numeric(self, header):
        return [_num(v) for v in self.columns[header]]

    def is_numeric(self, header):
        return _is_numeric_column(self.columns[header])

    def typed(self, header):
        """Column as floats/None when numeric, else stripped strings."""
        if self.is_numeric(header):
            return self.numeric(header)
        return [('' if v is None else str(v).strip()) for v in self.columns[header]]


_KV_RE = re.compile(r'^\s*#?\s*([A-Za-z_][\w .\-/()]*?)\s*:\s*(.*)$')


def parse_table(src, preamble=True):
    """Read a delimited text table.  Leading comment lines ('#') and a
    block of 'key: value' lines before the header are collected into
    ``preamble`` (a dict) when ``preamble`` is True."""
    text = _decode(_load_bytes(src))
    lines = text.splitlines()
    pre = {}
    pre_lines = []
    warnings = []
    start = 0
    if preamble:
        while start < len(lines):
            ln = lines[start]
            s = ln.strip()
            if not s:
                start += 1
                continue
            if s.startswith('#'):
                pre_lines.append(s)
                m = _KV_RE.match(s)
                if m:
                    pre[m.group(1).strip()] = m.group(2).strip()
                start += 1
                continue
            m = _KV_RE.match(s)
            if m and not any(d in m.group(1) for d in ',;\t|') and ':' in s:
                # 'key: value' header line (not a CSV header row)
                pre_lines.append(s)
                pre[m.group(1).strip()] = m.group(2).strip()
                start += 1
                continue
            break
    body = '\n'.join(lines[start:])
    if not body.strip():
        raise ValueError('empty table')
    dialect = sniff_dialect(body)
    reader = csv.reader(io.StringIO(body), dialect)
    rows = list(reader)
    rows = [r for r in rows if any(c.strip() for c in r)]
    if not rows:
        raise ValueError('table has no rows')
    headers = [h.strip() for h in rows[0]]
    # de-duplicate / fill blank headers
    seen = {}
    fixed = []
    for k, h in enumerate(headers):
        if not h:
            h = 'col%d' % (k + 1)
        if h in seen:
            seen[h] += 1
            h = '%s_%d' % (h, seen[h])
        else:
            seen[h] = 1
        fixed.append(h)
    headers = fixed
    ncol = len(headers)
    columns = {h: [] for h in headers}
    ragged = 0
    for r in rows[1:]:
        if len(r) != ncol:
            ragged += 1
            r = (r + [''] * ncol)[:ncol]
        for h, c in zip(headers, r):
            columns[h].append(c.strip())
    if ragged:
        warnings.append('%d row(s) had a different number of cells than the header (padded / truncated)' % ragged)
    t = Table(headers, columns, pre, warnings, dialect)
    t.preamble_lines = pre_lines
    return t


def _csv_text(header, rows, delimiter=','):
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=delimiter, lineterminator='\n')
    w.writerow(header)
    for r in rows:
        w.writerow([_fmt(v) for v in r])
    return buf.getvalue().encode('utf-8')


def _prov(fmt, path):
    return {'format': fmt, 'path': path}


# ------------------------------------------------------------------- points
def read_points_csv(src, x=None, y=None, z=None, name=None, role='points'):
    """Point table -> PointSet.  X / Y / Z columns are found by synonym
    (``x`` / ``east`` / ``easting`` ..., ``y`` / ``north`` ..., ``z`` /
    ``elev`` / ``rl`` ...) unless named explicitly.  Without a Z column
    every point gets Z = 0 (warned).  All other columns become attributes."""
    path = _src_path(src)
    t = parse_table(src)
    warnings = list(t.warnings)
    xc = t.find(X_SYN, x)
    yc = t.find(Y_SYN, y, exclude=(xc,))
    if xc is None or yc is None:
        raise ValueError('no X / Y columns found in %s' % t.headers)
    zc = t.find(Z_SYN, z, exclude=(xc, yc))
    if zc is None:
        warnings.append('no Z column (%s): Z set to 0' % ', '.join(Z_SYN[:4]))
    xs, ys = t.numeric(xc), t.numeric(yc)
    zs = t.numeric(zc) if zc else [0.0] * t.n
    attr_cols = [h for h in t.headers if h not in (xc, yc, zc)]
    typed = {h: t.typed(h) for h in attr_cols}
    # pre-create the columns so they keep the file's column order
    # (PointSet.add creates missing columns in set order)
    ps = PointSet(name=name or _stem(path, 'points'), role=role, provenance=_prov('csv_points', path),
                  attributes={h: [] for h in attr_cols})
    skipped = 0
    for i in range(t.n):
        if xs[i] is None or ys[i] is None:
            skipped += 1
            continue
        zi = zs[i] if zs[i] is not None else NAN
        ps.add(xs[i], ys[i], zi, **{h: typed[h][i] for h in attr_cols})
    if skipped:
        warnings.append('%d row(s) without numeric X/Y skipped' % skipped)
    ps.metadata['columns'] = {'x': xc, 'y': yc, 'z': zc}
    ps.metadata['warnings'] = warnings
    return ps


def write_points_csv(points, dst, columns=None, leapfrog=False):
    """PointSet -> 'x,y,z,<attrs>' CSV (``leapfrog=True`` -> 'East,North,Elev').
    ``columns`` limits / orders the attribute columns."""
    cols = list(columns) if columns is not None else list(points.attributes)
    header = (['East', 'North', 'Elev'] if leapfrog else ['x', 'y', 'z']) + cols
    rows = []
    for i in range(points.n):
        x, y, z = points.point(i)
        row = [x, y, z]
        for c in cols:
            col = points.attributes.get(c, [])
            row.append(col[i] if i < len(col) else None)
        rows.append(row)
    return _emit(dst, _csv_text(header, rows))


# --------------------------------------------------------------- drillholes
def _hole_id(v):
    s = '' if v is None else str(v).strip()
    if re.match(r'^-?\d+\.0$', s):
        s = s[:-2]
    return s


def read_drillholes(collar_src, survey_src=None, interval_srcs=None,
                    negative_dip_down=False, name=None):
    """Collar (+ survey + interval tables) CSVs -> Drillholes.

    Dips are stored positive-down (Leapfrog).  Pass ``negative_dip_down=True``
    when the survey file uses the negative-down convention (dips negated).
    ``interval_srcs`` = {'assay': path, 'lith': path, ...}; every column other
    than hole / from / to is kept (numeric when possible)."""
    cpath = _src_path(collar_src)
    warnings = []
    t = parse_table(collar_src)
    warnings.extend('collar: ' + w for w in t.warnings)
    hc = t.find(HOLE_SYN)
    if hc is None:
        raise ValueError('collar table: no hole id column in %s' % t.headers)
    xc = t.find(X_SYN, exclude=(hc,))
    yc = t.find(Y_SYN, exclude=(hc, xc))
    zc = t.find(Z_SYN, exclude=(hc, xc, yc))
    if xc is None or yc is None:
        raise ValueError('collar table: no X / Y columns in %s' % t.headers)
    if zc is None:
        warnings.append('collar: no Z column, collar elevations set to 0')
    dc = t.find(DEPTH_SYN, exclude=(hc, xc, yc, zc))
    if dc is None:
        warnings.append('collar: no depth column (depth / max_depth / eoh)')
    extra = [h for h in t.headers if h not in (hc, xc, yc, zc, dc)]
    typed = {h: t.typed(h) for h in extra}
    xs, ys = t.numeric(xc), t.numeric(yc)
    zs = t.numeric(zc) if zc else [0.0] * t.n
    ds = t.numeric(dc) if dc else [None] * t.n
    collars = []
    seen = set()
    for i in range(t.n):
        hole = _hole_id(t.columns[hc][i])
        if not hole or xs[i] is None or ys[i] is None:
            warnings.append('collar row %d skipped (missing hole id or coordinates)' % (i + 2))
            continue
        if hole in seen:
            warnings.append('duplicate collar %r (kept both)' % hole)
        seen.add(hole)
        rec = {'hole': hole, 'x': xs[i], 'y': ys[i], 'z': zs[i] if zs[i] is not None else 0.0,
               'depth': ds[i]}
        for h in extra:
            rec[h] = typed[h][i]
        collars.append(rec)

    surveys = []
    spath = _src_path(survey_src) if survey_src is not None else None
    if survey_src is not None:
        s = parse_table(survey_src)
        warnings.extend('survey: ' + w for w in s.warnings)
        shc = s.find(HOLE_SYN)
        sdc = s.find(SVY_DEPTH_SYN, exclude=(shc,))
        sac = s.find(AZI_SYN, exclude=(shc, sdc))
        sic = s.find(DIP_SYN, exclude=(shc, sdc, sac))
        if None in (shc, sdc, sac, sic):
            raise ValueError('survey table: need hole / depth / azimuth / dip columns, got %s' % s.headers)
        sd, sa, si = s.numeric(sdc), s.numeric(sac), s.numeric(sic)
        sextra = [h for h in s.headers if h not in (shc, sdc, sac, sic)]
        styped = {h: s.typed(h) for h in sextra}
        neg = pos = 0
        for i in range(s.n):
            hole = _hole_id(s.columns[shc][i])
            if not hole or sd[i] is None or si[i] is None:
                warnings.append('survey row %d skipped (missing hole / depth / dip)' % (i + 2))
                continue
            dip = -si[i] if negative_dip_down else si[i]
            if dip < 0:
                neg += 1
            elif dip > 0:
                pos += 1
            rec = {'hole': hole, 'depth': sd[i], 'azimuth': sa[i] if sa[i] is not None else 0.0, 'dip': dip}
            for h in sextra:
                rec[h] = styped[h][i]
            surveys.append(rec)
        if neg and not pos:
            warnings.append('survey: all dips are negative after conversion -- if the file uses '
                            'negative-down dips pass negative_dip_down=%s' % (not negative_dip_down))
        if not negative_dip_down and neg and pos:
            warnings.append('survey: mixed dip signs (%d negative, %d positive)' % (neg, pos))
        holes = {c['hole'] for c in collars}
        orphans = sorted({r['hole'] for r in surveys if r['hole'] not in holes})
        if orphans:
            warnings.append('survey: %d hole id(s) not in collar table (e.g. %s)' % (len(orphans), ', '.join(orphans[:5])))
    else:
        warnings.append('no survey table: holes are treated as vertical')

    intervals = {}
    ipaths = {}
    for table, isrc in (interval_srcs or {}).items():
        ipaths[table] = _src_path(isrc)
        it = parse_table(isrc)
        warnings.extend('%s: %s' % (table, w) for w in it.warnings)
        ihc = it.find(HOLE_SYN)
        ifc = it.find(FROM_SYN, exclude=(ihc,))
        itc = it.find(TO_SYN, exclude=(ihc, ifc))
        if None in (ihc, ifc, itc):
            raise ValueError('%s table: need hole / from / to columns, got %s' % (table, it.headers))
        fr, to = it.numeric(ifc), it.numeric(itc)
        iextra = [h for h in it.headers if h not in (ihc, ifc, itc)]
        ityped = {h: it.typed(h) for h in iextra}
        rows = []
        bad = 0
        for i in range(it.n):
            hole = _hole_id(it.columns[ihc][i])
            if not hole or fr[i] is None or to[i] is None:
                bad += 1
                continue
            if to[i] < fr[i]:
                warnings.append('%s: hole %s interval %g-%g reversed' % (table, hole, fr[i], to[i]))
            rec = {'hole': hole, 'from': fr[i], 'to': to[i]}
            for h in iextra:
                rec[h] = ityped[h][i]
            rows.append(rec)
        if bad:
            warnings.append('%s: %d row(s) without hole / from / to skipped' % (table, bad))
        holes = {c['hole'] for c in collars}
        orphans = sorted({r['hole'] for r in rows if r['hole'] not in holes})
        if orphans:
            warnings.append('%s: %d hole id(s) not in collar table (e.g. %s)' % (table, len(orphans), ', '.join(orphans[:5])))
        intervals[table] = rows

    dh = Drillholes(collars, surveys, intervals, name=name or _stem(cpath, 'drillholes'),
                    provenance={'format': 'csv_drillholes', 'path': cpath, 'survey': spath,
                                'intervals': ipaths})
    dh.metadata['columns'] = {'hole': hc, 'x': xc, 'y': yc, 'z': zc, 'depth': dc}
    dh.metadata['dip_convention'] = 'positive down'
    dh.metadata['warnings'] = warnings
    return dh


def write_drillholes(dh, dst):
    """Drillholes -> collar.csv / survey.csv / <table>.csv in directory ``dst``
    (created when missing; a path ending with a separator or an existing
    directory) or ``<prefix>_collar.csv`` ... when ``dst`` is a file prefix.
    Headers: 'holeid,x,y,z,max_depth', 'holeid,depth,azimuth,dip',
    'holeid,from,to,...'.  Returns {'collar': path, 'survey': path, <table>: path}."""
    dst = os.fspath(dst)
    if dst.endswith(os.sep) or dst.endswith('/') or os.path.isdir(dst):
        os.makedirs(dst, exist_ok=True)
        def target(n):
            return os.path.join(dst, n + '.csv')
    else:
        d = os.path.dirname(dst)
        if d:
            os.makedirs(d, exist_ok=True)
        def target(n):
            return '%s_%s.csv' % (dst, n)
    out = {}
    extra = []
    for c in dh.collars:
        for k in c:
            if k not in ('hole', 'x', 'y', 'z', 'depth') and k not in extra:
                extra.append(k)
    rows = [[c['hole'], c.get('x'), c.get('y'), c.get('z'), c.get('depth')] + [c.get(k) for k in extra]
            for c in dh.collars]
    p = target('collar')
    _emit(p, _csv_text(['holeid', 'x', 'y', 'z', 'max_depth'] + extra, rows))
    out['collar'] = p
    sextra = []
    for s in dh.surveys:
        for k in s:
            if k not in ('hole', 'depth', 'azimuth', 'dip') and k not in sextra:
                sextra.append(k)
    rows = [[s['hole'], s.get('depth'), s.get('azimuth'), s.get('dip')] + [s.get(k) for k in sextra]
            for s in dh.surveys]
    p = target('survey')
    _emit(p, _csv_text(['holeid', 'depth', 'azimuth', 'dip'] + sextra, rows))
    out['survey'] = p
    for table, recs in dh.intervals.items():
        cols = []
        for r in recs:
            for k in r:
                if k not in ('hole', 'from', 'to') and k not in cols:
                    cols.append(k)
        rows = [[r['hole'], r.get('from'), r.get('to')] + [r.get(k) for k in cols] for r in recs]
        safe = re.sub(r'[^\w\-]+', '_', str(table)) or 'intervals'
        p = target(safe)
        _emit(p, _csv_text(['holeid', 'from', 'to'] + cols, rows))
        out[table] = p
    return out


# --------------------------------------------------------------- structural
def read_structural_csv(src, name=None):
    """Planar structural measurements -> PointSet(role='structural') with
    numeric 'dip' and 'dip_azimuth' attributes (+ 'polarity' when present,
    kept as given).  A 'strike' column is converted with the right-hand rule
    (dip_azimuth = strike + 90) when no dip-direction column exists."""
    path = _src_path(src)
    t = parse_table(src)
    warnings = list(t.warnings)
    xc = t.find(X_SYN)
    yc = t.find(Y_SYN, exclude=(xc,))
    zc = t.find(Z_SYN, exclude=(xc, yc))
    if xc is None or yc is None:
        raise ValueError('structural table: no X / Y columns in %s' % t.headers)
    if zc is None:
        warnings.append('no Z column: Z set to 0')
    dipc = t.find(DIP_SYN, exclude=(xc, yc, zc))
    dazc = t.find(DIPAZ_SYN, exclude=(xc, yc, zc, dipc))
    strc = t.find(STRIKE_SYN, exclude=(xc, yc, zc, dipc, dazc))
    polc = t.find(POLARITY_SYN, exclude=(xc, yc, zc, dipc, dazc, strc))
    if dipc is None and dazc is None and strc is None:
        raise ValueError('structural table: no dip / dip_azimuth / strike columns in %s' % t.headers)
    if dipc is None:
        warnings.append('no dip column: dip set to NaN')
    xs, ys = t.numeric(xc), t.numeric(yc)
    zs = t.numeric(zc) if zc else [0.0] * t.n
    dips = t.numeric(dipc) if dipc else [None] * t.n
    if dazc is not None:
        dazs = t.numeric(dazc)
        if strc is not None:
            warnings.append('both %r and %r present: dip azimuth taken from %r' % (dazc, strc, dazc))
    elif strc is not None:
        strikes = t.numeric(strc)
        dazs = [None if s is None else (s + 90.0) % 360.0 for s in strikes]
        warnings.append('dip_azimuth derived from %r with the right-hand rule (strike + 90)' % strc)
    else:
        dazs = [None] * t.n
        warnings.append('no dip azimuth / strike column: dip_azimuth set to NaN')
    used = (xc, yc, zc, dipc, dazc, strc, polc)
    extra = [h for h in t.headers if h not in used]
    typed = {h: t.typed(h) for h in extra}
    pols = t.typed(polc) if polc else None
    order = ['dip', 'dip_azimuth'] + (['polarity'] if pols is not None else []) + extra
    ps = PointSet(name=name or _stem(path, 'structural'), role='structural',
                  provenance=_prov('csv_structural', path), attributes={h: [] for h in order})
    skipped = 0
    for i in range(t.n):
        if xs[i] is None or ys[i] is None:
            skipped += 1
            continue
        attrs = {'dip': NAN if dips[i] is None else dips[i],
                 'dip_azimuth': NAN if dazs[i] is None else dazs[i]}
        if pols is not None:
            attrs['polarity'] = pols[i]
        for h in extra:
            attrs[h] = typed[h][i]
        ps.add(xs[i], ys[i], zs[i] if zs[i] is not None else NAN, **attrs)
    if skipped:
        warnings.append('%d row(s) without numeric X/Y skipped' % skipped)
    ps.metadata['columns'] = {'x': xc, 'y': yc, 'z': zc, 'dip': dipc, 'dip_azimuth': dazc,
                              'strike': strc, 'polarity': polc}
    ps.metadata['warnings'] = warnings
    return ps


def write_structural_csv(points, dst):
    """PointSet -> 'x,y,z,dip,dip_azimuth,polarity[,<other attrs>]' CSV."""
    base = ['dip', 'dip_azimuth', 'polarity']
    extra = [k for k in points.attributes if k not in base]
    header = ['x', 'y', 'z'] + base + extra
    rows = []
    for i in range(points.n):
        x, y, z = points.point(i)
        row = [x, y, z]
        for c in base + extra:
            col = points.attributes.get(c, [])
            v = col[i] if i < len(col) else None
            row.append(v)
        rows.append(row)
    return _emit(dst, _csv_text(header, rows))


# -------------------------------------------------------------- block model
def _infer_spacing(values):
    """Minimum positive spacing of the sorted unique values (None if < 2)."""
    u = sorted(set(v for v in values if v is not None))
    best = None
    for a, b in zip(u, u[1:]):
        d = b - a
        if d > 1e-9 and (best is None or d < best):
            best = d
    return best


def _mode(values):
    counts = {}
    for v in values:
        if v is None:
            continue
        counts[v] = counts.get(v, 0) + 1
    if not counts:
        return None
    return max(counts, key=counts.get)


def _header_floats(value):
    try:
        return [float(v) for v in re.split(r'[,\s;x]+', str(value).strip()) if v != '']
    except ValueError:
        return None


def _header_get(pre, *keys):
    normed = {norm(k): v for k, v in pre.items()}
    for k in keys:
        if norm(k) in normed:
            return normed[norm(k)]
    return None


def read_blockmodel_csv(src, block_size=None, name=None):
    """Block-model CSV (one row per block: centroid x/y/z, optional dx/dy/dz
    and any number of attribute columns) -> BlockModel on a regular grid.

    Block size: ``block_size`` argument > embedded header ('block_size:') >
    size columns (most common value) > minimum positive spacing of the
    sorted unique centroids.  Origin / counts come from the embedded header
    when present (so a model whose edge rows were skipped keeps its extent),
    else from the centroid extents.  Rows that do not land on the lattice
    (tolerance 1e-6 * size) are skipped and warned; missing blocks are NaN.
    Leapfrog-style leading '#' / 'key: value' header lines are stored in
    ``metadata['header']``."""
    path = _src_path(src)
    t = parse_table(src)
    warnings = list(t.warnings)
    pre = dict(t.preamble)
    xc = t.find(BM_X_SYN)
    yc = t.find(BM_Y_SYN, exclude=(xc,))
    zc = t.find(BM_Z_SYN, exclude=(xc, yc))
    if None in (xc, yc, zc):
        raise ValueError('block model table: need centroid x / y / z columns, got %s' % t.headers)
    dxc = t.find(DX_SYN, exclude=(xc, yc, zc))
    dyc = t.find(DY_SYN, exclude=(xc, yc, zc, dxc))
    dzc = t.find(DZ_SYN, exclude=(xc, yc, zc, dxc, dyc))
    xs, ys, zs = t.numeric(xc), t.numeric(yc), t.numeric(zc)

    # --- block size
    size = None
    size_source = None
    if block_size is not None:
        size = [float(block_size)] * 3 if isinstance(block_size, (int, float)) else [float(v) for v in block_size]
        size_source = 'argument'
    if size is None:
        hv = _header_get(pre, 'block_size', 'blocksize', 'block size', 'size', 'cell_size', 'cellsize')
        fl = _header_floats(hv) if hv is not None else None
        if fl:
            size = (fl * 3)[:3] if len(fl) < 3 else fl[:3]
            size_source = 'embedded header'
    if size is None and None not in (dxc, dyc, dzc):
        cols = [t.numeric(dxc), t.numeric(dyc), t.numeric(dzc)]
        size = [_mode(c) for c in cols]
        if None in size:
            size = None
        else:
            size_source = 'size columns'
            for c, nm in zip(cols, (dxc, dyc, dzc)):
                distinct = sorted(set(v for v in c if v is not None))
                if len(distinct) > 1:
                    warnings.append('column %r has %d distinct sizes (%s): sub-blocked model regularised '
                                    'to the most common size' % (nm, len(distinct), ', '.join('%g' % d for d in distinct[:6])))
    if size is None:
        size = [_infer_spacing(xs), _infer_spacing(ys), _infer_spacing(zs)]
        size_source = 'inferred from centroid spacing'
        for a, nm in enumerate('xyz'):
            if size[a] is None:
                size[a] = 1.0
                warnings.append('cannot infer %s block size (single layer): using 1.0' % nm)
    size = [float(v) for v in size]
    if min(size) <= 0:
        raise ValueError('block size must be positive: %r' % size)

    # --- rotation (only recoverable from an embedded header)
    azimuth = 0.0
    hv = _header_get(pre, 'azimuth', 'rotation', 'bearing')
    if hv is not None:
        fl = _header_floats(hv)
        if fl:
            azimuth = fl[0]

    # --- origin + counts
    valid = [i for i in range(t.n) if None not in (xs[i], ys[i], zs[i])]
    if not valid:
        raise ValueError('block model table: no rows with numeric centroids')
    origin = None
    count = None
    hv = _header_get(pre, 'base_point', 'basepoint', 'origin', 'base point', 'min_corner', 'minimum')
    fl = _header_floats(hv) if hv is not None else None
    if fl and len(fl) >= 3:
        origin = fl[:3]
    hv = _header_get(pre, 'count', 'counts', 'blocks', 'n_blocks', 'dimensions', 'nx_ny_nz', 'size_in_blocks')
    fl = _header_floats(hv) if hv is not None else None
    if fl and len(fl) >= 3 and all(v >= 1 and float(v).is_integer() for v in fl[:3]):
        count = [int(v) for v in fl[:3]]
    # local (unrotated) coordinates of each centroid
    if azimuth and origin is None:
        warnings.append('header azimuth %g ignored: no origin / base point in header' % azimuth)
        azimuth = 0.0
    if azimuth:
        r = math.radians(azimuth)
        c, s = math.cos(r), math.sin(r)
        ox, oy = origin[0], origin[1]
        loc = [((xs[i] - ox) * c - (ys[i] - oy) * s, (xs[i] - ox) * s + (ys[i] - oy) * c, zs[i] - origin[2])
               for i in valid]
        # local u/v/w are offsets from the origin
        lx = [p[0] for p in loc]
        ly = [p[1] for p in loc]
        lz = [p[2] for p in loc]
        local_origin = [0.0, 0.0, 0.0]
    else:
        lx = [xs[i] for i in valid]
        ly = [ys[i] for i in valid]
        lz = [zs[i] for i in valid]
        local_origin = origin
    if local_origin is None:
        local_origin = [min(lx) - size[0] / 2.0, min(ly) - size[1] / 2.0, min(lz) - size[2] / 2.0]
        origin = list(local_origin)
    if count is None:
        count = [int(round((max(v) - o - sz / 2.0) / sz)) + 1
                 for v, o, sz in ((lx, local_origin[0], size[0]), (ly, local_origin[1], size[1]), (lz, local_origin[2], size[2]))]
        count = [max(1, c) for c in count]
    bm = BlockModel(origin, size, count, azimuth=azimuth, name=name or _stem(path, 'blockmodel'),
                    provenance=_prov('csv_blockmodel', path))
    n = bm.n
    if n > 50_000_000:
        raise ValueError('block model too large: %d blocks' % n)

    # --- place rows on the lattice
    attr_cols = [h for h in t.headers if h not in (xc, yc, zc, dxc, dyc, dzc)]
    typed = {h: t.typed(h) for h in attr_cols}
    is_num = {h: t.is_numeric(h) for h in attr_cols}
    store = {}
    for h in attr_cols:
        store[h] = farray([NAN]) * n if is_num[h] else [None] * n
    tol = [1e-6 * sz for sz in size]
    off_lattice = 0
    out_of_range = 0
    dup = 0
    filled = set()
    for k, i in enumerate(valid):
        ijk = []
        ok = True
        for a, (v, o, sz) in enumerate(((lx[k], local_origin[0], size[0]), (ly[k], local_origin[1], size[1]),
                                        (lz[k], local_origin[2], size[2]))):
            f = (v - o) / sz - 0.5
            idx = int(round(f))
            if abs(f - idx) * sz > tol[a]:
                ok = False
                break
            ijk.append(idx)
        if not ok:
            off_lattice += 1
            continue
        if any(ijk[a] < 0 or ijk[a] >= count[a] for a in range(3)):
            out_of_range += 1
            continue
        idx = bm.index(*ijk)
        if idx in filled:
            dup += 1
        filled.add(idx)
        for h in attr_cols:
            v = typed[h][i]
            if is_num[h]:
                store[h][idx] = NAN if v is None else v
            else:
                store[h][idx] = v
    for h in attr_cols:
        bm.add_attribute(h, store[h], kind='number' if is_num[h] else 'text')
    if off_lattice:
        warnings.append('%d row(s) do not land on the %gx%gx%g lattice and were skipped' % (off_lattice, size[0], size[1], size[2]))
    if out_of_range:
        warnings.append('%d row(s) fall outside the header-defined grid and were skipped' % out_of_range)
    if dup:
        warnings.append('%d duplicate block position(s): last row wins' % dup)
    missing = n - len(filled)
    if missing:
        warnings.append('%d of %d blocks have no row (NaN)' % (missing, n))
    bm.metadata['header'] = pre
    bm.metadata['block_size_source'] = size_source
    bm.metadata['columns'] = {'x': xc, 'y': yc, 'z': zc, 'dx': dxc, 'dy': dyc, 'dz': dzc}
    bm.metadata['rows'] = t.n
    bm.metadata['warnings'] = warnings
    return bm


def blockmodel_definition(bm, rows=None):
    """Ordered (key, value) pairs describing the grid (sidecar / header)."""
    items = [('name', bm.name),
             ('base_point', '%s, %s, %s' % tuple(_fmt(v) for v in bm.origin)),
             ('block_size', '%s, %s, %s' % tuple(_fmt(v) for v in bm.block_size)),
             ('count', '%d, %d, %d' % tuple(bm.count)),
             ('azimuth', _fmt(bm.azimuth)),
             ('blocks', str(bm.n))]
    if rows is not None:
        items.append(('rows', str(rows)))
    items.append(('generator', 'nw-mineral-monitor geomodel'))
    return items


def write_blockmodel_csv(bm, dst, attributes=None, embedded_header=True, sidecar=True, skip_empty=True):
    """BlockModel -> 'x,y,z,dx,dy,dz,<attrs>' CSV of block centroids.

    ``skip_empty`` drops blocks whose attributes are all NaN / None;
    ``embedded_header`` prepends '# key: value' definition lines;
    ``sidecar`` also writes '<dst>.txt' with the same definition in plain
    'key: value' lines (path destinations only).  Returns the path (bytes
    for a BytesIO)."""
    names = list(attributes) if attributes is not None else list(bm.attributes)
    cols = [(nm, bm.attributes[nm]['values']) for nm in names]
    header = ['x', 'y', 'z', 'dx', 'dy', 'dz'] + names
    dx, dy, dz = bm.block_size
    rows = []
    nx, ny, nz = bm.count
    for k in range(nz):
        for j in range(ny):
            for i in range(nx):
                idx = bm.index(i, j, k)
                vals = [c[idx] for _, c in cols]
                if skip_empty and cols and all(v is None or (isinstance(v, float) and v != v) or v == '' for v in vals):
                    continue
                x, y, z = bm.centroid(i, j, k)
                rows.append([x, y, z, dx, dy, dz] + vals)
    definition = blockmodel_definition(bm, rows=len(rows))
    body = _csv_text(header, rows)
    if embedded_header:
        body = ''.join('# %s: %s\n' % kv for kv in definition).encode('utf-8') + body
    out = _emit(dst, body)
    if sidecar and not hasattr(dst, 'write'):
        with open(os.fspath(dst) + '.txt', 'w', encoding='utf-8') as fh:
            for kv in definition:
                fh.write('%s: %s\n' % kv)
    return out
