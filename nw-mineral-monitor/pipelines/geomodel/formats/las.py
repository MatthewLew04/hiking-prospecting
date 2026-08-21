"""CWLS LAS well logs (1.2 / 2.0, best-effort 3.0), stdlib only.

``read_las`` -> dict: 'version', 'wrap', 'delimiter', 'well' {mnem:
{'unit', 'value', 'descr'}}, 'curves' [{'mnem', 'unit', 'descr'}], 'params'
{...}, 'other' (str), 'data' {mnem: array('d')} with NULL (default -999.25)
-> NaN, 'index_unit', 'null', 'warnings', 'path'.  Handles WRAP YES
(multi-line records) and NO, '#' comments, duplicate mnemonics (suffixed
':1', ':2' ... like lasio) and LAS 3.0's ~Version DLM plus the first
~Log_Definition / ~Log_Data pair (comma / tab / space delimited).

``las_to_intervals`` turns the log into Drillholes-style interval rows
(each sample = [depth - step/2, depth + step/2]) so logs can be attached
as ``Drillholes.intervals['las']``.  ``write_las`` is a minimal LAS 2.0
WRAP NO writer.
"""
import array
import io
import os
import re

FORMAT_ID = 'las'
NAN = float('nan')
DEFAULT_NULL = -999.25


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


_TIME_COLON = re.compile(r'\d:\d')


def parse_header_line(line):
    """'MNEM.UNIT  VALUE : DESCR' -> (mnem, unit, value, descr).  The value
    ends at the LAST colon (LAS 3.0 rule, what lasio does) so times like
    12:30:00 survive; a line without a period is 'MNEM : VALUE'."""
    s = line.strip()
    if '.' not in s.split(':', 1)[0]:
        # no period before the first colon: name : value
        if ':' in s:
            name, val = s.split(':', 1)
            return name.strip(), '', val.strip(), ''
        return s, '', '', ''
    name, rest = s.split('.', 1)
    name = name.strip()
    # unit: up to the first whitespace or colon
    m = re.match(r'([^\s:]*)(.*)$', rest, re.S)
    unit = m.group(1)
    rest = m.group(2)
    if ':' in rest:
        k = rest.rfind(':')
        value, descr = rest[:k], rest[k + 1:]
    else:
        value, descr = rest, ''
    return name, unit.strip(), value.strip(), descr.strip()


def _section_kind(title):
    """Map a '~Section' title to one of version / well / curve / param /
    other / data / unknown (LAS 1.2, 2.0 and 3.0 names)."""
    t = title.lstrip('~').strip().lower()
    m = re.match(r'[a-z_]+', t.replace(' ', '_', 1) if t.startswith('log ') else t)
    word = m.group(0) if m else ''
    if not word:
        return 'unknown'
    if word == 'v' or word.startswith('version'):
        return 'version'
    if word == 'w' or word.startswith('well'):
        return 'well'
    if word == 'c' or word.startswith('curve') or word.startswith('log_definition'):
        return 'curve'
    if word == 'p' or word.startswith('param') or word.startswith('log_parameter'):
        return 'param'
    if word == 'o' or word.startswith('other'):
        return 'other'
    if word == 'a' or word.startswith('ascii') or word.startswith('log_data'):
        return 'data'
    if word.startswith('log'):
        return 'log_other'
    return 'unknown'


def _num(tok, null):
    try:
        v = float(tok)
    except ValueError:
        return NAN
    if null is not None and abs(v - null) <= 1e-9 * max(1.0, abs(null)):
        return NAN
    return v


def _split_data_line(line, delimiter):
    if delimiter == ',':
        parts = [p.strip() for p in line.split(',')]
    elif delimiter == '\t':
        parts = [p.strip() for p in line.split('\t')]
    else:
        parts = line.split()
    return parts


# ------------------------------------------------------------------- reader
def read_las(src):
    """Parse a LAS file (path / bytes / file object).  See module docstring."""
    path = _src_path(src)
    text = _decode(_load_bytes(src))
    lines = text.splitlines()
    warnings = []
    version = None
    wrap = False
    delimiter = ' '
    well = {}
    curves = []
    params = {}
    other = []
    data_lines = []
    section = None
    data_sections = 0
    seen_sections = []
    for raw in lines:
        line = raw.rstrip('\r\n')
        s = line.strip()
        if not s:
            continue
        if s.startswith('~'):
            kind = _section_kind(s)
            seen_sections.append(s.split()[0])
            if kind == 'data':
                data_sections += 1
                if data_sections > 1:
                    warnings.append('%d data sections: only the first was read' % data_sections)
                    section = 'skip'
                    continue
            elif kind == 'curve' and curves:
                # a second ~Curve / ~Log_Definition block (LAS 3.0 multi-set): ignore
                warnings.append('additional curve-definition section %r ignored' % s)
                section = 'skip'
                continue
            elif kind in ('unknown', 'log_other'):
                warnings.append('section %r not understood; skipped' % s.split()[0])
                section = 'skip'
                continue
            section = kind
            continue
        if section == 'data':
            if s.startswith('#'):
                continue
            data_lines.append(s)
            continue
        if s.startswith('#'):
            continue
        if section == 'skip' or section is None:
            continue
        if section == 'other':
            other.append(line.rstrip())
            continue
        name, unit, value, descr = parse_header_line(s)
        if section == 'version':
            u = name.upper()
            if u == 'VERS':
                try:
                    version = float(value)
                except ValueError:
                    version = None
                    warnings.append('unparsable VERS %r' % value)
            elif u == 'WRAP':
                wrap = value.strip().upper().startswith('Y')
            elif u == 'DLM':
                dv = value.strip().upper()
                delimiter = {'COMMA': ',', 'TAB': '\t', 'SPACE': ' '}.get(dv, ' ')
                if dv not in ('COMMA', 'TAB', 'SPACE', ''):
                    warnings.append('unknown DLM %r: whitespace assumed' % value)
            else:
                params.setdefault('_version', {})[name] = {'unit': unit, 'value': value, 'descr': descr}
        elif section == 'well':
            well[name.upper()] = {'unit': unit, 'value': value, 'descr': descr}
        elif section == 'curve':
            curves.append({'mnem': name, 'unit': unit, 'descr': descr, 'value': value})
        elif section == 'param':
            params[name] = {'unit': unit, 'value': value, 'descr': descr}

    if version is None:
        warnings.append('no ~Version section / VERS line')
    if version is not None and version >= 3:
        warnings.append('LAS 3.0 read best-effort (first ~Log_Data set, DLM honoured)')
    if wrap and version is not None and version >= 3:
        warnings.append('WRAP YES is not legal in LAS 3.0; honoured anyway')
    if not curves:
        warnings.append('no ~Curve section: data columns unnamed')
    null = DEFAULT_NULL
    nv = well.get('NULL', {}).get('value')
    if nv not in (None, ''):
        try:
            null = float(nv)
        except ValueError:
            warnings.append('unparsable NULL %r: %g assumed' % (nv, DEFAULT_NULL))

    # de-duplicate mnemonics (lasio style)
    names = []
    counts = {}
    for c in curves:
        m = c['mnem']
        if m in counts:
            counts[m] += 1
            m2 = '%s:%d' % (m, counts[m])
            if counts[m] == 2:
                # rename the first occurrence as well
                first = names.index(m)
                names[first] = '%s:1' % m
            names.append(m2)
        else:
            counts[m] = 1
            names.append(m)
    if any(counts[m] > 1 for m in counts):
        warnings.append('duplicate curve mnemonics renamed with :1, :2 suffixes')

    # data
    ncur = len(names)
    records = []
    if wrap and ncur:
        buf = []
        for ln in data_lines:
            buf.extend(_split_data_line(ln, delimiter))
            while len(buf) >= ncur:
                records.append(buf[:ncur])
                buf = buf[ncur:]
        if buf:
            warnings.append('%d leftover value(s) at the end of the wrapped data section' % len(buf))
    else:
        short = long = 0
        for ln in data_lines:
            toks = _split_data_line(ln, delimiter)
            if ncur == 0:
                ncur = len(toks)
                names = ['COL%d' % (k + 1) for k in range(ncur)]
                curves = [{'mnem': n, 'unit': '', 'descr': ''} for n in names]
            if len(toks) < ncur:
                short += 1
                toks = toks + [''] * (ncur - len(toks))
            elif len(toks) > ncur:
                long += 1
                toks = toks[:ncur]
            records.append(toks)
        if short:
            warnings.append('%d data line(s) with fewer values than curves (padded with NULL)' % short)
        if long:
            warnings.append('%d data line(s) with more values than curves (truncated)' % long)
    data = {n: array.array('d') for n in names}
    for rec in records:
        for n, tok in zip(names, rec):
            data[n].append(_num(tok, null) if tok != '' else NAN)
    if not records:
        warnings.append('no data rows')
    index_unit = curves[0]['unit'] if curves else ''
    out = {'version': version, 'wrap': wrap, 'delimiter': delimiter, 'well': well,
           'curves': [{'mnem': n, 'unit': c.get('unit', ''), 'descr': c.get('descr', '')}
                      for n, c in zip(names, curves)],
           'params': params, 'other': '\n'.join(other), 'data': data, 'index_unit': index_unit,
           'null': null, 'n_rows': len(records), 'sections': seen_sections, 'warnings': warnings,
           'path': path}
    return out


def las_to_intervals(d, hole, step=None, curves=None):
    """Log samples -> interval rows [{'hole', 'from', 'to', <curve>: value}]
    (NaN -> None).  ``step`` defaults to the ~Well STEP or the median depth
    increment; each sample spans [depth - step/2, depth + step/2]."""
    names = [c['mnem'] for c in d['curves']]
    if not names:
        return []
    index = d['data'][names[0]]
    n = len(index)
    if step is None:
        sv = d['well'].get('STEP', {}).get('value')
        try:
            step = abs(float(sv)) if sv not in (None, '') else 0.0
        except ValueError:
            step = 0.0
        if not step:
            diffs = sorted(abs(index[k + 1] - index[k]) for k in range(n - 1)
                           if index[k] == index[k] and index[k + 1] == index[k + 1])
            step = diffs[len(diffs) // 2] if diffs else 1.0
    step = abs(float(step))
    wanted = [c for c in names[1:] if curves is None or c in curves]
    rows = []
    for k in range(n):
        depth = index[k]
        if depth != depth:
            continue
        r = {'hole': hole, 'from': depth - step / 2.0, 'to': depth + step / 2.0}
        for c in wanted:
            v = d['data'][c][k]
            r[c] = None if v != v else v
        rows.append(r)
    return rows


# ------------------------------------------------------------------- writer
def _fmt(v, width=None):
    if v is None or (isinstance(v, float) and v != v):
        return ''
    if isinstance(v, float):
        r = '%.10g' % v
        if r in ('', '-', '-0'):
            r = '0'
    else:
        r = str(v)
    return r.rjust(width) if width else r


def write_las(d, dst):
    """Minimal LAS 2.0 (WRAP NO) writer for a dict shaped like ``read_las``
    output (at least 'curves' and 'data'; 'well' / 'params' / 'other'
    optional).  Returns the path (bytes for a BytesIO)."""
    curves = d.get('curves') or []
    data = d.get('data') or {}
    if not curves:
        raise ValueError('write_las: no curves')
    names = [c['mnem'] for c in curves]
    for n in names:
        if n not in data:
            raise ValueError('write_las: no data for curve %r' % n)
    n = len(data[names[0]])
    null = d.get('null', DEFAULT_NULL)
    try:
        null = float(null)
    except (TypeError, ValueError):
        null = DEFAULT_NULL
    index = data[names[0]]
    well = dict(d.get('well') or {})
    idx_unit = curves[0].get('unit', '') or well.get('STRT', {}).get('unit', '')
    strt = index[0] if n else 0.0
    stop = index[-1] if n else 0.0
    step = (index[1] - index[0]) if n > 1 else 0.0
    if n > 2:
        diffs = [index[k + 1] - index[k] for k in range(n - 1)]
        if max(diffs) - min(diffs) > 1e-6 * max(1.0, abs(step)):
            step = 0.0               # irregular sampling
    L = []
    w = L.append
    w('~Version Information')
    w(' VERS.                 2.0 : CWLS LOG ASCII STANDARD - VERSION 2.0')
    w(' WRAP.                  NO : ONE LINE PER DEPTH STEP')
    w('~Well Information')
    w('#MNEM.UNIT       DATA                    DESCRIPTION')
    w('#---- -----      ----------------------  ---------------------------')

    def item(mnem, unit, value, descr):
        w(' %-5s.%-7s %22s : %s' % (mnem, unit, _fmt(value), descr))

    item('STRT', idx_unit, strt, 'START DEPTH')
    item('STOP', idx_unit, stop, 'STOP DEPTH')
    item('STEP', idx_unit, step, 'STEP')
    item('NULL', '', null, 'NULL VALUE')
    for mnem in ('COMP', 'WELL', 'FLD', 'LOC', 'PROV', 'CNTY', 'STAT', 'CTRY', 'SRVC', 'DATE', 'UWI', 'API'):
        if mnem in well:
            item(mnem, well[mnem].get('unit', ''), well[mnem].get('value', ''), well[mnem].get('descr', ''))
        elif mnem in ('COMP', 'WELL', 'FLD', 'LOC', 'SRVC', 'DATE', 'UWI'):
            item(mnem, '', '', {'COMP': 'COMPANY', 'WELL': 'WELL', 'FLD': 'FIELD', 'LOC': 'LOCATION',
                                'SRVC': 'SERVICE COMPANY', 'DATE': 'LOG DATE', 'UWI': 'UNIQUE WELL ID'}[mnem])
    for mnem, it in well.items():
        if mnem in ('STRT', 'STOP', 'STEP', 'NULL', 'COMP', 'WELL', 'FLD', 'LOC', 'PROV', 'CNTY', 'STAT',
                    'CTRY', 'SRVC', 'DATE', 'UWI', 'API'):
            continue
        item(mnem, it.get('unit', ''), it.get('value', ''), it.get('descr', ''))
    w('~Curve Information')
    w('#MNEM.UNIT       API CODE                DESCRIPTION')
    w('#---- -----      ----------------------  ---------------------------')
    for c in curves:
        item(c['mnem'], c.get('unit', ''), c.get('value', ''), c.get('descr', ''))
    params = {k: v for k, v in (d.get('params') or {}).items() if not k.startswith('_')}
    if params:
        w('~Parameter Information')
        for mnem, it in params.items():
            item(mnem, it.get('unit', ''), it.get('value', ''), it.get('descr', ''))
    other = d.get('other')
    if other:
        w('~Other')
        for ln in str(other).splitlines():
            w(ln)
    w('~ASCII Log Data')
    cols = [data[nm] for nm in names]
    for k in range(n):
        cells = []
        for col in cols:
            v = col[k] if k < len(col) else NAN
            if v is None or v != v:
                v = null
            cells.append(_fmt(float(v), 12))
        w(' '.join(cells))
    return _emit(dst, ('\n'.join(L) + '\n').encode('ascii', 'replace'))
