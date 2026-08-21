"""SEG-Y rev 0 / 1 / 2 seismic (or GPR / resistivity) sections, stdlib only.

``read_segy`` returns a plain dict (the section is image-like data, not a
model object): the decoded textual header (EBCDIC or ASCII, auto-detected),
the binary header fields that matter, per-trace headers with the coordinate
scalar applied, per-trace sample arrays and (x, y) per trace (CDP X/Y when
set, else source X/Y).  ``section_image`` turns that into the pieces an
``ImagePlane`` of kind 'section' needs: an 8-bit grey image (row 0 = time
0) plus the first / last trace coordinates and z_top / z_bottom.

Sample formats: 1 IBM float (exact bit conversion), 2 int32, 3 int16,
5 IEEE float32, 6 IEEE float64, 8 int8, 9 int64, 10 uint32, 11 uint16,
12 uint64, 16 uint8.  Byte order: big-endian unless the rev-2 byte-order
word at 3297-3300 says otherwise (or ``endian`` is given); a little-endian
guess is made (and warned) when the big-endian format code is impossible.
Extended textual headers (count at 3505-3506, -1 = until '((SEG: EndText))')
are skipped.  Fixed-length flag 0 -> per-trace sample counts.  All byte
positions in this module are the 1-based numbers of the standard.

``write_segy`` writes a minimal rev 1, big-endian file (ASCII textual
header, IEEE float by default, CDP X/Y in 181/185 with scalar 1, inline /
crossline in 189/193, shotpoint in 197) -- enough for round trips and for
segyio / OpendTect / Geosoft to read.
"""
import array
import io
import math
import os
import struct

FORMAT_ID = 'segy'

TEXT_LEN = 3200
BIN_LEN = 400
TRACE_HDR_LEN = 240
_FORMAT_SIZE = {1: 4, 2: 4, 3: 2, 5: 4, 6: 8, 8: 1, 9: 8, 10: 4, 11: 2, 12: 8, 16: 1}
_FORMAT_CODE = {2: 'i', 3: 'h', 5: 'f', 6: 'd', 8: 'b', 9: 'q', 10: 'I', 11: 'H', 12: 'Q', 16: 'B'}
_FORMAT_NAME = {1: 'IBM float32', 2: 'int32', 3: 'int16', 4: 'fixed-point with gain (unsupported)',
                5: 'IEEE float32', 6: 'IEEE float64', 7: 'int24 (unsupported)', 8: 'int8', 9: 'int64',
                10: 'uint32', 11: 'uint16', 12: 'uint64', 15: 'uint24 (unsupported)', 16: 'uint8'}
_IBM_POW16 = [16.0 ** (e - 64) for e in range(128)]
_BYTE_ORDER_BIG = 16909060        # 0x01020304 read as big-endian
_BYTE_ORDER_LITTLE = 67305985     # 0x04030201 == 0x01020304 written little-endian, read big


# ------------------------------------------------------------------ helpers
def _load_bytes(src):
    if isinstance(src, (bytes, bytearray, memoryview)):
        return bytes(src)
    if hasattr(src, 'read'):
        data = src.read()
        return data if isinstance(data, bytes) else data.encode('latin-1')
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


def ibm_to_float(word):
    """IBM System/360 single precision -> float.  Bit 31 sign, bits 30-24
    base-16 exponent biased by 64, bits 23-0 fraction:
    value = (-1)^s * (frac / 2^24) * 16^(e - 64)."""
    if word == 0:
        return 0.0
    s = word >> 31
    e = (word >> 24) & 0x7f
    f = word & 0xffffff
    v = (f / 16777216.0) * _IBM_POW16[e]
    return -v if s else v


def float_to_ibm(v):
    """float -> IBM single precision word (round-to-nearest, overflow clamps)."""
    v = float(v)
    if v == 0.0 or v != v:
        return 0
    s = 0x80000000 if v < 0 else 0
    v = abs(v)
    m, x = math.frexp(v)               # v = m * 2**x, 0.5 <= m < 1
    x4 = -((-x) // 4)                  # ceil(x / 4)
    f = m * 2.0 ** (x - 4 * x4)        # in [1/16, 1)
    e = x4 + 64
    frac = int(round(f * 16777216.0))
    if frac >= 16777216:
        frac >>= 4
        e += 1
    while frac and e < 0:
        frac >>= 4
        e += 1
    if e > 127:
        e, frac = 127, 0xffffff
    return s | (e << 24) | (frac & 0xffffff)


def _decode_text(raw):
    """3200-byte textual header -> 40 lines.  ASCII if byte 0 is 'C' (0x43)
    or the block looks like printable ASCII, else EBCDIC (cp037)."""
    if raw[:1] == b'C':
        txt = raw.decode('ascii', 'replace')
        kind = 'ascii'
    else:
        # EBCDIC letters / digits all live above 0x80 (space is 0x40): a
        # block with no high bytes that is not just EBCDIC blanks is ASCII
        high = any(b >= 0x80 for b in raw)
        blank = all(b in (0x40, 0x00) for b in raw)
        if not high and not blank:
            txt = raw.decode('ascii', 'replace')
            kind = 'ascii'
        else:
            txt = raw.decode('cp037', 'replace')
            kind = 'ebcdic'
    lines = [txt[i:i + 80].rstrip() for i in range(0, len(txt), 80)]
    return '\n'.join(lines), kind


def _u(fmt, data, off):
    return struct.unpack_from(fmt, data, off)[0]


def _scaled(v, scalar):
    if scalar > 0:
        return v * scalar
    if scalar < 0:
        return v / float(-scalar)
    return float(v)


def _decode_samples(buf, fmt, ns, bo):
    if fmt == 1:
        words = struct.unpack('%s%dI' % (bo, ns), buf)
        out = array.array('d', [0.0]) * ns
        for k, w in enumerate(words):
            if w:
                s = w >> 31
                v = ((w & 0xffffff) / 16777216.0) * _IBM_POW16[(w >> 24) & 0x7f]
                out[k] = -v if s else v
        return out
    tc = _FORMAT_CODE[fmt]
    vals = struct.unpack('%s%d%s' % (bo, ns, tc), buf)
    return array.array('d', vals)


# ------------------------------------------------------------------- reader
def read_segy(src, iline_byte=189, xline_byte=193, cdpx_byte=181, cdpy_byte=185, sp_byte=197,
              endian=None, max_traces=None):
    """Parse a SEG-Y file (path / bytes / file object) -> dict with keys
    text_header, text_encoding, binary_header, n_traces, samples (list of
    array('d')), dt (s), trace_headers (list of dicts), coords (list of
    (x, y)), ns, format, endian, warnings, path.

    ``iline_byte`` ... ``sp_byte`` are the 1-based header byte positions of
    the 4-byte inline / crossline / CDP-X / CDP-Y / shotpoint fields."""
    path = _src_path(src)
    data = _load_bytes(src)
    warnings = []
    if len(data) < TEXT_LEN + BIN_LEN:
        raise ValueError('SEG-Y: file shorter than the 3600-byte file header')
    text, text_kind = _decode_text(data[:TEXT_LEN])
    b = data[TEXT_LEN:TEXT_LEN + BIN_LEN]

    # --- byte order
    if endian in ('big', '>'):
        bo = '>'
    elif endian in ('little', '<'):
        bo = '<'
    else:
        word = _u('>I', b, 96)                 # bytes 3297-3300
        if word == _BYTE_ORDER_BIG:
            bo = '>'
        elif word == _BYTE_ORDER_LITTLE:
            bo = '<'
        else:
            fmt_big = _u('>h', b, 24)
            fmt_little = _u('<h', b, 24)
            if fmt_big in _FORMAT_SIZE:
                bo = '>'
            elif fmt_little in _FORMAT_SIZE:
                bo = '<'
                warnings.append('format code only valid as little-endian: reading the file little-endian')
            else:
                bo = '>'
                warnings.append('format code %d is not valid in either byte order' % fmt_big)

    fmt = _u(bo + 'h', b, 24)
    ns_hdr = _u(bo + 'H', b, 20)
    dt_us = _u(bo + 'H', b, 16)
    rev_raw = _u(bo + 'H', b, 300)
    revision = (rev_raw >> 8) + (rev_raw & 0xff) / 100.0 if rev_raw else 0.0
    binary_header = {
        'job_id': _u(bo + 'i', b, 0), 'line_number': _u(bo + 'i', b, 4), 'reel_number': _u(bo + 'i', b, 8),
        'traces_per_ensemble': _u(bo + 'h', b, 12), 'aux_traces_per_ensemble': _u(bo + 'h', b, 14),
        'sample_interval_us': dt_us, 'sample_interval_orig_us': _u(bo + 'H', b, 18),
        'samples_per_trace': ns_hdr, 'samples_per_trace_orig': _u(bo + 'H', b, 22),
        'format_code': fmt, 'format': _FORMAT_NAME.get(fmt, 'unknown (%d)' % fmt),
        'ensemble_fold': _u(bo + 'h', b, 26), 'trace_sorting': _u(bo + 'h', b, 28),
        'measurement_system': _u(bo + 'h', b, 54),
        'revision': revision, 'revision_raw': rev_raw,
        'fixed_length': _u(bo + 'h', b, 302), 'n_ext_text': _u(bo + 'h', b, 304),
    }
    if rev_raw >= 0x0200:
        binary_header['max_extra_trace_headers'] = _u(bo + 'i', b, 306)
        binary_header['n_traces_in_file'] = _u(bo + 'Q', b, 312)
        binary_header['first_trace_offset'] = _u(bo + 'Q', b, 320)
    if fmt not in _FORMAT_SIZE:
        raise ValueError('SEG-Y: unsupported data sample format code %d (%s)' % (fmt, _FORMAT_NAME.get(fmt, '?')))
    size = _FORMAT_SIZE[fmt]

    # --- extended textual headers
    off = TEXT_LEN + BIN_LEN
    n_ext = binary_header['n_ext_text']
    if n_ext > 0:
        off += n_ext * TEXT_LEN
        warnings.append('%d extended textual header(s) skipped' % n_ext)
    elif n_ext < 0:
        count = 0
        while off + TEXT_LEN <= len(data):
            blk = data[off:off + TEXT_LEN]
            off += TEXT_LEN
            count += 1
            if b'((SEG: EndText))' in blk or b'((SEG: EndText))'.decode('ascii').encode('cp037') in blk:
                break
        warnings.append('%d extended textual header(s) (variable count) skipped' % count)
    first_off = binary_header.get('first_trace_offset', 0)
    if first_off and first_off > off and first_off < len(data):
        off = first_off
    if binary_header['fixed_length'] == 0 and revision >= 1:
        warnings.append('fixed-length trace flag is 0: per-trace sample counts honoured')

    # --- traces
    samples = []
    headers = []
    coords = []
    n_bad = 0
    ntr = 0
    extra_hdrs = binary_header.get('max_extra_trace_headers', 0) or 0
    while off + TRACE_HDR_LEN <= len(data):
        if max_traces is not None and ntr >= max_traces:
            break
        h = data[off:off + TRACE_HDR_LEN]
        ns = _u(bo + 'H', h, 114)
        if binary_header['fixed_length'] == 1 and ns_hdr:
            if ns != ns_hdr and ns:
                n_bad += 1
            ns = ns_hdr
        elif ns == 0:
            ns = ns_hdr
        tdt = _u(bo + 'H', h, 116)
        hdr_bytes = TRACE_HDR_LEN
        if extra_hdrs and rev_raw >= 0x0200:
            # rev 2: optional 240-byte trace header extensions (count at 157-158)
            hdr_bytes += TRACE_HDR_LEN * max(0, _u(bo + 'h', h, 156))
        start = off + hdr_bytes
        end = start + ns * size
        if end > len(data):
            warnings.append('trace %d truncated (needs %d bytes, %d left): stopped' % (ntr + 1, ns * size, len(data) - start))
            break
        sc = _u(bo + 'h', h, 70)
        se = _u(bo + 'h', h, 68)
        th = {
            'seq': _u(bo + 'i', h, 0), 'seq_file': _u(bo + 'i', h, 4), 'ffid': _u(bo + 'i', h, 8),
            'trace_in_ffid': _u(bo + 'i', h, 12), 'energy_source_point': _u(bo + 'i', h, 16),
            'cdp': _u(bo + 'i', h, 20), 'trace_in_cdp': _u(bo + 'i', h, 24),
            'trace_id': _u(bo + 'h', h, 28), 'offset': _u(bo + 'i', h, 36),
            'rec_elev': _scaled(_u(bo + 'i', h, 40), se), 'src_elev': _scaled(_u(bo + 'i', h, 44), se),
            'src_depth': _scaled(_u(bo + 'i', h, 48), se),
            'scalar_elev': se, 'scalar_coord': sc,
            'sx': _scaled(_u(bo + 'i', h, 72), sc), 'sy': _scaled(_u(bo + 'i', h, 76), sc),
            'gx': _scaled(_u(bo + 'i', h, 80), sc), 'gy': _scaled(_u(bo + 'i', h, 84), sc),
            'coord_units': _u(bo + 'h', h, 88),
            'delay_ms': _u(bo + 'h', h, 108), 'ns': ns, 'dt_us': tdt,
            'cdpx': _scaled(_u(bo + 'i', h, cdpx_byte - 1), sc), 'cdpy': _scaled(_u(bo + 'i', h, cdpy_byte - 1), sc),
            'inline': _u(bo + 'i', h, iline_byte - 1), 'xline': _u(bo + 'i', h, xline_byte - 1),
            'sp': _u(bo + 'i', h, sp_byte - 1),
        }
        headers.append(th)
        samples.append(_decode_samples(data[start:end], fmt, ns, bo))
        if th['cdpx'] or th['cdpy']:
            coords.append((th['cdpx'], th['cdpy']))
        else:
            coords.append((th['sx'], th['sy']))
        off = end
        ntr += 1
    if n_bad:
        warnings.append('%d trace header(s) disagree with the binary-header sample count (binary header used)' % n_bad)
    if off < len(data) and not (max_traces is not None and ntr >= max_traces):
        warnings.append('%d trailing byte(s) after the last trace ignored' % (len(data) - off))
    if not samples:
        warnings.append('no traces')
    if dt_us == 0 and headers:
        dt_us = headers[0]['dt_us']
        warnings.append('binary-header sample interval is 0: using the first trace header (%d us)' % dt_us)
    if dt_us == 0:
        warnings.append('sample interval unknown (0)')
    ns_all = sorted({len(s) for s in samples}) if samples else [ns_hdr]
    if len(ns_all) > 1:
        warnings.append('variable trace lengths: %s samples' % ', '.join(str(v) for v in ns_all[:6]))
    if all(c == (0, 0) for c in coords):
        warnings.append('no trace coordinates (CDP X/Y and source X/Y all zero)')
    return {
        'text_header': text, 'text_encoding': text_kind, 'binary_header': binary_header,
        'n_traces': len(samples), 'samples': samples, 'dt': dt_us * 1e-6, 'trace_headers': headers,
        'coords': coords, 'ns': max(ns_all) if ns_all else 0, 'format': fmt, 'endian': 'big' if bo == '>' else 'little',
        'revision': revision, 'warnings': warnings, 'path': path,
    }


def section_image(d, z_top=None, z_bottom=None, clip_pct=98.0):
    """Amplitude section -> 8-bit grey image (bytes, row-major, width =
    traces, height = samples, row 0 = time 0) with the first / last trace
    coordinates as p1 / p2 and z_top / z_bottom.  Without a depth conversion
    z_top = 0 and z_bottom = -(ns - 1) * dt * 1000 (two-way time in ms used
    as pseudo-depth, flagged in 'warnings').  Amplitudes are clipped at the
    ``clip_pct`` percentile of |amplitude|; 128 = zero."""
    samples = d['samples']
    width = len(samples)
    height = max((len(s) for s in samples), default=0)
    warnings = []
    dt = d.get('dt') or 0.0
    if z_top is None:
        z_top = 0.0
    if z_bottom is None:
        z_bottom = -(max(height - 1, 0)) * dt * 1000.0
        warnings.append('no depth conversion: z is two-way time in ms (negative down) as pseudo-depth')
    # clip level from a subsample of |amplitude|
    mags = []
    total = sum(len(s) for s in samples)
    step = max(1, total // 200000)
    k = 0
    for s in samples:
        for v in s:
            if k % step == 0 and v == v:
                mags.append(abs(v))
            k += 1
    mags.sort()
    clip = 0.0
    if mags:
        pct = min(max(float(clip_pct), 0.0), 100.0)
        idx = min(len(mags) - 1, int(round(pct / 100.0 * (len(mags) - 1))))
        clip = mags[idx]
    if clip <= 0:
        clip = max(mags) if mags and max(mags) > 0 else 1.0
        if not any(mags):
            warnings.append('all amplitudes are zero')
    gray = bytearray(b'\x80' * (width * height))
    scale = 127.0 / clip
    for col, s in enumerate(samples):
        for row, v in enumerate(s):
            if v != v:
                continue
            g = 128 + int(v * scale)
            gray[row * width + col] = 0 if g < 0 else (255 if g > 255 else g)
    coords = d.get('coords') or []
    p1 = list(coords[0]) if coords else None
    p2 = list(coords[-1]) if coords else None
    if p1 is not None and p1 == p2 and width > 1:
        warnings.append('first and last trace share the same coordinates')
    return {'width': width, 'height': height, 'gray': bytes(gray), 'p1': p1, 'p2': p2,
            'z_top': z_top, 'z_bottom': z_bottom, 'clip': clip, 'dt': dt, 'warnings': warnings}


segy_to_section = section_image      # alias: ImagePlane(kind='section')-ready pieces


# ------------------------------------------------------------------- writer
def _text_header(text, encoding='ascii'):
    lines = []
    if text:
        lines = [ln.rstrip() for ln in str(text).splitlines()]
    out = []
    for k in range(40):
        body = lines[k] if k < len(lines) else ''
        if not body.startswith('C'):
            body = ('C%2d %s' % (k + 1, body)).rstrip()
        out.append(body[:80].ljust(80))
    blob = ''.join(out)
    if encoding == 'ebcdic':
        return blob.encode('cp037', 'replace')
    return blob.encode('ascii', 'replace')


def write_segy(samples, dst, dt_us, coords=None, format_code=5, text=None, inlines=None,
               xlines=None, delay_ms=0, endian='big', text_encoding='ascii'):
    """Write traces (sequence of per-trace sequences of floats, equal
    length) as a minimal SEG-Y rev 1 file.  ``coords`` = [(x, y)] per trace
    written as CDP X/Y (181/185) and source X/Y (73/77) with scalar 1 (values
    rounded to integers).  ``format_code`` 1 (IBM), 2, 3, 5 (default), 8.
    ``text_encoding`` 'ascii' (default) or 'ebcdic' (what segyio and most
    legacy readers assume).  Returns the path (bytes for a BytesIO)."""
    bo = '>' if endian == 'big' else '<'
    if text_encoding not in ('ascii', 'ebcdic'):
        raise ValueError("text_encoding must be 'ascii' or 'ebcdic'")
    traces = [list(t) for t in samples]
    ntr = len(traces)
    ns = len(traces[0]) if traces else 0
    if any(len(t) != ns for t in traces):
        raise ValueError('write_segy: all traces must have the same number of samples')
    if format_code not in (1, 2, 3, 5, 8):
        raise ValueError('write_segy: format_code must be 1, 2, 3, 5 or 8')
    if ns > 65535:
        raise ValueError('write_segy: more than 65535 samples per trace')
    dt_us = int(round(dt_us))
    head = text
    if head is None:
        head = ('nw-mineral-monitor geomodel SEG-Y rev 1 export\n'
                'TRACES %d  SAMPLES %d  SAMPLE INTERVAL %d US\n'
                'BYTES 181-184 CDP X, 185-188 CDP Y (SCALAR 1), 189-192 INLINE, 193-196 CROSSLINE\n'
                'BYTES 197-200 SHOTPOINT, 73-76 SOURCE X, 77-80 SOURCE Y\n'
                'DATA FORMAT %d\nEND TEXTUAL HEADER' % (ntr, ns, dt_us, format_code))
    out = bytearray(_text_header(head, text_encoding))
    b = bytearray(BIN_LEN)
    struct.pack_into(bo + 'i', b, 0, 1)           # job id
    struct.pack_into(bo + 'i', b, 4, 1)           # line number
    struct.pack_into(bo + 'i', b, 8, 1)           # reel number
    struct.pack_into(bo + 'h', b, 12, 1)          # traces per ensemble
    struct.pack_into(bo + 'H', b, 16, dt_us)
    struct.pack_into(bo + 'H', b, 18, dt_us)
    struct.pack_into(bo + 'H', b, 20, ns)
    struct.pack_into(bo + 'H', b, 22, ns)
    struct.pack_into(bo + 'h', b, 24, format_code)
    struct.pack_into(bo + 'h', b, 26, 1)          # fold
    struct.pack_into(bo + 'h', b, 28, 4)          # trace sorting: stacked
    struct.pack_into(bo + 'h', b, 54, 1)          # metres
    struct.pack_into(bo + 'H', b, 300, 0x0100)    # rev 1.0
    struct.pack_into(bo + 'h', b, 302, 1)         # fixed length traces
    struct.pack_into(bo + 'h', b, 304, 0)         # no extended text headers
    out += b
    for k, t in enumerate(traces):
        h = bytearray(TRACE_HDR_LEN)
        x, y = (coords[k] if coords is not None and k < len(coords) else (0.0, 0.0))
        xi, yi = int(round(x)), int(round(y))
        il = int(inlines[k]) if inlines is not None else 1
        xl = int(xlines[k]) if xlines is not None else k + 1
        struct.pack_into(bo + 'i', h, 0, k + 1)       # seq line
        struct.pack_into(bo + 'i', h, 4, k + 1)       # seq file
        struct.pack_into(bo + 'i', h, 8, k + 1)       # ffid
        struct.pack_into(bo + 'i', h, 12, 1)          # trace in ffid
        struct.pack_into(bo + 'i', h, 16, k + 1)      # energy source point
        struct.pack_into(bo + 'i', h, 20, k + 1)      # cdp
        struct.pack_into(bo + 'i', h, 24, 1)          # trace in cdp
        struct.pack_into(bo + 'h', h, 28, 1)          # trace id: seismic
        struct.pack_into(bo + 'h', h, 68, 1)          # elevation scalar
        struct.pack_into(bo + 'h', h, 70, 1)          # coordinate scalar
        struct.pack_into(bo + 'i', h, 72, xi)
        struct.pack_into(bo + 'i', h, 76, yi)
        struct.pack_into(bo + 'i', h, 80, xi)
        struct.pack_into(bo + 'i', h, 84, yi)
        struct.pack_into(bo + 'h', h, 88, 1)          # coordinate units: length
        struct.pack_into(bo + 'h', h, 108, int(delay_ms))
        struct.pack_into(bo + 'H', h, 114, ns)
        struct.pack_into(bo + 'H', h, 116, dt_us)
        struct.pack_into(bo + 'i', h, 180, xi)
        struct.pack_into(bo + 'i', h, 184, yi)
        struct.pack_into(bo + 'i', h, 188, il)
        struct.pack_into(bo + 'i', h, 192, xl)
        struct.pack_into(bo + 'i', h, 196, k + 1)     # shotpoint
        struct.pack_into(bo + 'h', h, 200, 1)         # shotpoint scalar
        out += h
        if format_code == 1:
            out += struct.pack('%s%dI' % (bo, ns), *[float_to_ibm(v) for v in t])
        elif format_code == 5:
            out += struct.pack('%s%df' % (bo, ns), *[float(v) for v in t])
        elif format_code == 2:
            out += struct.pack('%s%di' % (bo, ns), *[max(-2147483648, min(2147483647, int(round(v)))) for v in t])
        elif format_code == 3:
            out += struct.pack('%s%dh' % (bo, ns), *[max(-32768, min(32767, int(round(v)))) for v in t])
        else:
            out += struct.pack('%s%db' % (bo, ns), *[max(-128, min(127, int(round(v)))) for v in t])
    return _emit(dst, bytes(out))
