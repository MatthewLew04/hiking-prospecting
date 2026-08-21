"""parquet_lite — a minimal, dependency-free Apache Parquet reader/writer.

Written for the OMF v2 container (``formats/omf2.py``) whose arrays are small
single-table Parquet files with a handful of fixed schemas, but general enough
to read what pyarrow / parquet-rs / parquet-cpp write for flat tables:

Writer (``write_parquet``)
  * one data page (v1) per column chunk per row group, PLAIN encoding,
    definition levels RLE/bit-packed hybrid, GZIP (default) or no compression;
  * required / optional leaves and one level of optional group nesting
    (``optional group vector { required double x; ... }``);
  * logical types: STRING, INTEGER(bits, signed), DATE, TIMESTAMP(micros/
    millis/nanos, UTC) — converted_type is filled in for old readers;
  * footer in Thrift compact protocol; root ``schema`` element carries no
    repetition, exactly like parquet-rs (the omf-rust reader compares the
    schema tree for equality, so this matters).

Reader (``read_parquet``)
  * data pages v1 and v2, dictionary pages, PLAIN / RLE_DICTIONARY /
    PLAIN_DICTIONARY / RLE(boolean) encodings, RLE or BIT_PACKED levels;
  * codecs UNCOMPRESSED and GZIP always; SNAPPY / ZSTD / LZ4 / BROTLI when
    ``cramjam`` (or ``python-snappy`` / ``zstandard``) is importable;
  * any depth of optional group nesting, multiple row groups;
  * repeated fields (lists/maps) are not supported and raise ``ParquetError``.

Values come back as plain Python lists (``None`` for nulls) keyed by the
dotted column path, e.g. ``{'vector.x': [...], 'vector.y': [...]}``.
"""
import array
import gzip
import io
import struct
import sys
import zlib

from . import thrift_compact as tc

PARQUET_MAGIC = b'PAR1'

# physical types
BOOLEAN, INT32, INT64, INT96, FLOAT, DOUBLE, BYTE_ARRAY, FIXED_LEN_BYTE_ARRAY = range(8)
PHYSICAL_NAMES = {'boolean': BOOLEAN, 'int32': INT32, 'int64': INT64, 'int96': INT96,
                  'float': FLOAT, 'double': DOUBLE, 'byte_array': BYTE_ARRAY,
                  'fixed_len_byte_array': FIXED_LEN_BYTE_ARRAY}
PHYSICAL_BY_CODE = dict((v, k) for k, v in PHYSICAL_NAMES.items())
# encodings
ENC_PLAIN, ENC_PLAIN_DICTIONARY, ENC_RLE, ENC_BIT_PACKED = 0, 2, 3, 4
ENC_DELTA_BINARY_PACKED, ENC_DELTA_LENGTH_BYTE_ARRAY, ENC_DELTA_BYTE_ARRAY = 5, 6, 7
ENC_RLE_DICTIONARY, ENC_BYTE_STREAM_SPLIT = 8, 9
# codecs
CODEC_UNCOMPRESSED, CODEC_SNAPPY, CODEC_GZIP, CODEC_LZO, CODEC_BROTLI, CODEC_LZ4, CODEC_ZSTD, CODEC_LZ4_RAW = range(8)
CODEC_NAMES = {'none': CODEC_UNCOMPRESSED, 'uncompressed': CODEC_UNCOMPRESSED,
               'gzip': CODEC_GZIP, 'snappy': CODEC_SNAPPY, 'zstd': CODEC_ZSTD}
# repetition
REQUIRED, OPTIONAL, REPEATED = 0, 1, 2
# page types
PAGE_DATA, PAGE_INDEX, PAGE_DICTIONARY, PAGE_DATA_V2 = 0, 1, 2, 3
# converted types (parquet.thrift ConvertedType enum — verified against
# parquet-rs / parquet-cpp output: UTF8 0, DATE 6, TIMESTAMP_MILLIS 9,
# TIMESTAMP_MICROS 10, UINT_8 11 .. UINT_64 14, INT_8 15 .. INT_64 18)
CT_UTF8, CT_DATE, CT_TIMESTAMP_MILLIS, CT_TIMESTAMP_MICROS = 0, 6, 9, 10
CT_UINT = {8: 11, 16: 12, 32: 13, 64: 14}
CT_INT = {8: 15, 16: 16, 32: 17, 64: 18}
# LogicalType union field ids
LT_STRING, LT_MAP, LT_LIST, LT_ENUM, LT_DECIMAL, LT_DATE, LT_TIME, LT_TIMESTAMP = 1, 2, 3, 4, 5, 6, 7, 8
LT_INTEGER, LT_UNKNOWN, LT_JSON, LT_BSON, LT_UUID = 10, 11, 12, 13, 14
# TimeUnit union field ids
TU_MILLIS, TU_MICROS, TU_NANOS = 1, 2, 3

DEFAULT_ROW_GROUP_SIZE = 1024 * 1024
CREATED_BY = 'nwmm geomodel parquet_lite'


class ParquetError(ValueError):
    """Unreadable or unsupported Parquet data."""


# ==================================================================== schema
class Column(object):
    """A leaf column for ``write_parquet``.

    ``ptype``: 'boolean' | 'int32' | 'int64' | 'float' | 'double' | 'byte_array'.
    ``logical``: None | 'string' | 'uint8' | 'uint16' | 'uint32' | 'uint64' |
    'int8' | 'int16' | 'date' | 'timestamp_micros' | 'timestamp_millis' |
    'timestamp_nanos'.
    ``values``: sequence; ``None`` marks a null (only for optional columns).
    """

    def __init__(self, name, ptype, values, optional=False, logical=None):
        self.name = name
        self.ptype = ptype if isinstance(ptype, int) else PHYSICAL_NAMES[ptype]
        self.values = values
        self.optional = bool(optional)
        self.logical = logical

    def __len__(self):
        return len(self.values)


class Group(object):
    """A (normally optional) group of leaf columns for ``write_parquet``.
    ``present``: sequence of bools, one per row (``None`` = all present)."""

    def __init__(self, name, children, optional=True, present=None):
        self.name = name
        self.children = list(children)
        self.optional = bool(optional)
        self.present = present

    def __len__(self):
        return len(self.children[0]) if self.children else 0


class Node(object):
    """Schema tree node (reader side)."""

    def __init__(self, name, repetition=None, ptype=None, type_length=None,
                 converted=None, logical=None, field_id=None, children=None):
        self.name = name
        self.repetition = repetition
        self.ptype = ptype
        self.type_length = type_length
        self.converted = converted
        self.logical = logical       # parsed: ('string',) | ('integer', bits, signed) | ('date',) | ('timestamp', unit, utc) | ('raw', {..})
        self.field_id = field_id
        self.children = children or []
        self.path = ()
        self.max_def = 0
        self.max_rep = 0

    @property
    def is_leaf(self):
        return self.ptype is not None

    def describe(self, indent=0):
        """Parquet-style schema text in the parquet-rs ``print_schema``
        layout, e.g. ``message schema {\\n  REQUIRED DOUBLE scalar;\\n}``."""
        pad = '  ' * indent
        rep = {REQUIRED: 'REQUIRED', OPTIONAL: 'OPTIONAL', REPEATED: 'REPEATED'}.get(self.repetition)
        if self.is_leaf:
            ann = ' (%s)' % _logical_text(self.logical) if self.logical else ''
            return '%s%s %s %s%s;' % (pad, rep or 'REQUIRED', PHYSICAL_BY_CODE[self.ptype].upper(), self.name, ann)
        if indent == 0:
            head = '%smessage %s {' % (pad, self.name)
        else:
            head = '%s%sgroup %s {' % (pad, (rep + ' ') if rep else '', self.name)
        lines = [head]
        for c in self.children:
            lines.append(c.describe(indent + 1))
        lines.append(pad + '}')
        return '\n'.join(lines)


def _logical_text(lt):
    if lt[0] == 'string':
        return 'STRING'
    if lt[0] == 'integer':
        return 'INTEGER(%d,%s)' % (lt[1], 'true' if lt[2] else 'false')
    if lt[0] == 'date':
        return 'DATE'
    if lt[0] == 'timestamp':
        return 'TIMESTAMP(%s,%s)' % (lt[1].upper(), 'true' if lt[2] else 'false')
    return str(lt)


def _parse_logical(raw, converted):
    """Thrift LogicalType union dict -> tuple; falls back on converted type."""
    if raw:
        if LT_STRING in raw:
            return ('string',)
        if LT_INTEGER in raw:
            d = raw[LT_INTEGER]
            return ('integer', int(d.get(1, 32)), bool(d.get(2, True)))
        if LT_DATE in raw:
            return ('date',)
        if LT_TIMESTAMP in raw:
            d = raw[LT_TIMESTAMP]
            unit = d.get(2, {})
            name = 'micros' if TU_MICROS in unit else 'millis' if TU_MILLIS in unit else 'nanos' if TU_NANOS in unit else 'micros'
            return ('timestamp', name, bool(d.get(1, True)))
        return ('raw', raw)
    if converted is None:
        return None
    if converted == CT_UTF8:
        return ('string',)
    if converted == CT_DATE:
        return ('date',)
    if converted == CT_TIMESTAMP_MILLIS:
        return ('timestamp', 'millis', True)
    if converted == CT_TIMESTAMP_MICROS:
        return ('timestamp', 'micros', True)
    for bits, code in CT_UINT.items():
        if code == converted:
            return ('integer', bits, False)
    for bits, code in CT_INT.items():
        if code == converted:
            return ('integer', bits, True)
    return ('converted', converted)


# ==================================================================== codecs
def _gzip_compress(data, level=6):
    return gzip.compress(data, compresslevel=level, mtime=0)


def _gzip_decompress(data):
    # gzip members (what parquet writers emit); fall back to raw zlib
    try:
        return gzip.decompress(data)
    except (OSError, EOFError, zlib.error):
        return zlib.decompress(data)


def _decompress(codec, data, expected=None):
    if codec == CODEC_UNCOMPRESSED:
        return data
    if codec == CODEC_GZIP:
        return _gzip_decompress(data)
    if codec == CODEC_SNAPPY:
        try:
            import cramjam
            return bytes(cramjam.snappy.decompress_raw(data))
        except ImportError:
            pass
        try:
            import snappy
            return snappy.uncompress(data)
        except ImportError:
            raise ParquetError('SNAPPY-compressed parquet needs the cramjam or python-snappy package')
    if codec == CODEC_ZSTD:
        try:
            import cramjam
            return bytes(cramjam.zstd.decompress(data))
        except ImportError:
            pass
        try:
            import zstandard
            return zstandard.ZstdDecompressor().decompress(data, max_output_size=expected or 0)
        except ImportError:
            raise ParquetError('ZSTD-compressed parquet needs the cramjam or zstandard package')
    if codec in (CODEC_LZ4, CODEC_LZ4_RAW):
        try:
            import cramjam
            if codec == CODEC_LZ4_RAW:
                return bytes(cramjam.lz4.decompress_block(data, expected))
            return bytes(cramjam.lz4.decompress(data))
        except ImportError:
            raise ParquetError('LZ4-compressed parquet needs the cramjam package')
    if codec == CODEC_BROTLI:
        try:
            import cramjam
            return bytes(cramjam.brotli.decompress(data))
        except ImportError:
            raise ParquetError('BROTLI-compressed parquet needs the cramjam package')
    raise ParquetError('unsupported parquet compression codec %d' % codec)


# ============================================================ RLE / bit-pack
def _bit_width(max_value):
    w = 0
    while (1 << w) <= max_value and max_value > 0:
        w += 1
    return w


def rle_hybrid_encode(values, bit_width):
    """RLE / bit-packed hybrid encoding (no length prefix)."""
    out = bytearray()
    n = len(values)
    if bit_width == 0 or n == 0:
        return bytes(out)
    literal = []
    nbytes = (bit_width + 7) // 8

    def flush_literal(final):
        if not literal:
            return
        if final and len(literal) % 8:
            literal.extend([0] * (8 - len(literal) % 8))
        assert len(literal) % 8 == 0
        groups = len(literal) // 8
        out.extend(tc.encode_varint((groups << 1) | 1))
        acc = 0
        shift = 0
        for v in literal:
            acc |= v << shift
            shift += bit_width
        out.extend(acc.to_bytes(groups * bit_width, 'little'))
        del literal[:]

    i = 0
    while i < n:
        v = values[i]
        j = i + 1
        while j < n and values[j] == v:
            j += 1
        run = j - i
        if run >= 8:
            pad = (-len(literal)) % 8
            if pad:
                if run - pad >= 8:
                    literal.extend([v] * pad)
                    i += pad
                    run -= pad
                else:
                    literal.extend(values[i:j])
                    i = j
                    continue
            flush_literal(False)
            out.extend(tc.encode_varint(run << 1))
            out.extend(int(v).to_bytes(nbytes, 'little'))
            i = j
        else:
            literal.extend(values[i:j])
            i = j
    flush_literal(True)
    return bytes(out)


def rle_hybrid_decode(buf, pos, end, bit_width, count):
    """Decode ``count`` values; -> (list, new_pos)."""
    out = []
    if bit_width == 0:
        return [0] * count, pos
    mask = (1 << bit_width) - 1
    nbytes = (bit_width + 7) // 8
    while len(out) < count and pos < end:
        header, pos = tc.decode_varint(buf, pos)
        if header & 1:
            groups = header >> 1
            total = groups * bit_width
            if pos + total > end:
                raise ParquetError('truncated bit-packed run')
            need = count - len(out)
            # chunk to keep the big integers small
            step = 64  # groups per chunk
            for g0 in range(0, groups, step):
                g1 = min(groups, g0 + step)
                chunk = buf[pos + g0 * bit_width:pos + g1 * bit_width]
                acc = int.from_bytes(chunk, 'little')
                nvals = min((g1 - g0) * 8, need)
                if bit_width == 1:
                    out.extend((acc >> k) & 1 for k in range(nvals))
                else:
                    out.extend((acc >> (k * bit_width)) & mask for k in range(nvals))
                need -= nvals
                if need <= 0:
                    break
            pos += total
        else:
            run = header >> 1
            if pos + nbytes > end:
                raise ParquetError('truncated RLE run')
            v = int.from_bytes(buf[pos:pos + nbytes], 'little') & mask
            pos += nbytes
            out.extend([v] * min(run, count - len(out)))
    if len(out) < count:
        raise ParquetError('not enough level/index data (%d of %d)' % (len(out), count))
    return out, pos


def _bitpacked_deprecated_decode(buf, pos, bit_width, count):
    """Deprecated BIT_PACKED level encoding: values packed MSB first."""
    total = (bit_width * count + 7) // 8
    chunk = buf[pos:pos + total]
    if len(chunk) < total:
        raise ParquetError('truncated BIT_PACKED levels')
    acc = int.from_bytes(chunk, 'big')
    nbits = total * 8
    mask = (1 << bit_width) - 1
    out = [(acc >> (nbits - (k + 1) * bit_width)) & mask for k in range(count)]
    return out, pos + total


# ============================================================= PLAIN codec
_STRUCT_FMT = {INT32: 'i', INT64: 'q', FLOAT: 'f', DOUBLE: 'd'}
_ARRAY_TYPECODE = {INT32: 'i', INT64: 'q', FLOAT: 'f', DOUBLE: 'd'}


def _plain_encode(ptype, values, logical=None):
    if ptype == BOOLEAN:
        out = bytearray((len(values) + 7) // 8)
        for k, v in enumerate(values):
            if v:
                out[k >> 3] |= 1 << (k & 7)
        return bytes(out)
    if ptype in _STRUCT_FMT:
        fmt = _STRUCT_FMT[ptype]
        if ptype == INT32 and logical in ('uint32', 'uint8', 'uint16'):
            fmt = 'I'
        elif ptype == INT64 and logical == 'uint64':
            fmt = 'Q'
        if ptype in (FLOAT, DOUBLE):
            a = array.array(fmt, (float(v) for v in values))
        elif fmt in 'IQ':
            a = array.array(fmt, (int(v) for v in values))
        else:
            a = array.array(fmt, (int(v) for v in values))
        if sys.byteorder != 'little':
            a.byteswap()
        return a.tobytes()
    if ptype == BYTE_ARRAY:
        out = bytearray()
        for v in values:
            if isinstance(v, str):
                v = v.encode('utf-8')
            out += struct.pack('<I', len(v))
            out += v
        return bytes(out)
    raise ParquetError('cannot PLAIN-encode physical type %d' % ptype)


def _plain_decode(ptype, buf, pos, count, type_length=None, as_text=False):
    """-> (list, new_pos)."""
    if ptype == BOOLEAN:
        nb = (count + 7) // 8
        chunk = buf[pos:pos + nb]
        if len(chunk) < nb:
            raise ParquetError('truncated boolean data')
        acc = int.from_bytes(chunk, 'little')
        return [bool((acc >> k) & 1) for k in range(count)], pos + nb
    if ptype in _ARRAY_TYPECODE:
        a = array.array(_ARRAY_TYPECODE[ptype])
        nb = count * a.itemsize
        chunk = buf[pos:pos + nb]
        if len(chunk) < nb:
            raise ParquetError('truncated numeric data')
        a.frombytes(bytes(chunk))
        if sys.byteorder != 'little':
            a.byteswap()
        return a.tolist(), pos + nb
    if ptype == INT96:
        nb = count * 12
        chunk = buf[pos:pos + nb]
        return [bytes(chunk[k * 12:(k + 1) * 12]) for k in range(count)], pos + nb
    if ptype == FIXED_LEN_BYTE_ARRAY:
        tl = type_length or 0
        nb = count * tl
        chunk = buf[pos:pos + nb]
        out = [bytes(chunk[k * tl:(k + 1) * tl]) for k in range(count)]
        return out, pos + nb
    if ptype == BYTE_ARRAY:
        out = []
        for _ in range(count):
            if pos + 4 > len(buf):
                raise ParquetError('truncated byte array')
            n = struct.unpack_from('<I', buf, pos)[0]
            pos += 4
            v = bytes(buf[pos:pos + n])
            pos += n
            if as_text:
                v = v.decode('utf-8', 'replace')
            out.append(v)
        return out, pos
    raise ParquetError('unsupported physical type %d' % ptype)


# ==================================================================== writer
class _Leaf(object):
    def __init__(self, column, path, chain):
        self.column = column
        self.path = path            # tuple of names
        self.chain = chain          # list of Group ancestors (outermost first)
        self.max_def = sum(1 for g in chain if g.optional) + (1 if column.optional else 0)


def _flatten_columns(fields):
    leaves = []
    elements = []   # thrift SchemaElement field lists (without the root)

    def visit(field, chain):
        if isinstance(field, Group):
            elements.append([(3, 'i32', OPTIONAL if field.optional else REQUIRED),
                             (4, 'binary', field.name), (5, 'i32', len(field.children))])
            for c in field.children:
                visit(c, chain + [field])
        else:
            ct, lt = _logical_fields(field.logical, field.ptype)
            elements.append([(1, 'i32', field.ptype),
                             (3, 'i32', OPTIONAL if field.optional else REQUIRED),
                             (4, 'binary', field.name), (6, 'i32', ct), (10, 'struct', lt)])
            leaves.append(_Leaf(field, tuple(g.name for g in chain) + (field.name,), list(chain)))

    for f in fields:
        visit(f, [])
    return leaves, elements


def _logical_fields(logical, ptype):
    """-> (converted_type or None, LogicalType union field list or None)."""
    if logical is None:
        return None, None
    if logical == 'string':
        if ptype != BYTE_ARRAY:
            raise ParquetError('string logical type needs byte_array')
        return CT_UTF8, [(LT_STRING, 'struct', [])]
    if logical.startswith('uint') or logical.startswith('int'):
        signed = not logical.startswith('u')
        bits = int(logical.lstrip('uint'))
        ct = (CT_INT if signed else CT_UINT)[bits]
        return ct, [(LT_INTEGER, 'struct', [(1, 'byte', bits), (2, 'bool', signed)])]
    if logical == 'date':
        return CT_DATE, [(LT_DATE, 'struct', [])]
    if logical.startswith('timestamp'):
        unit = logical.split('_', 1)[1] if '_' in logical else 'micros'
        tu = {'millis': TU_MILLIS, 'micros': TU_MICROS, 'nanos': TU_NANOS}[unit]
        ct = {'millis': CT_TIMESTAMP_MILLIS, 'micros': CT_TIMESTAMP_MICROS}.get(unit)
        return ct, [(LT_TIMESTAMP, 'struct', [(1, 'bool', True), (2, 'struct', [(tu, 'struct', [])])])]
    raise ParquetError('unknown logical type %r' % logical)


def _def_levels(leaf, start, stop):
    """Definition levels + the present values for rows [start, stop)."""
    col = leaf.column
    vals = col.values
    present = []
    levels = []
    opt_groups = [g for g in leaf.chain if g.optional]
    if not opt_groups and not col.optional:
        return None, vals[start:stop]
    if not opt_groups:
        for i in range(start, stop):
            v = vals[i]
            if v is None:
                levels.append(0)
            else:
                levels.append(1)
                present.append(v)
        return levels, present
    for i in range(start, stop):
        d = 0
        absent = False
        for g in opt_groups:
            if g.present is None or g.present[i]:
                d += 1
            else:
                absent = True
                break
        if not absent:
            v = vals[i]
            if col.optional:
                if v is None:
                    absent = True
                else:
                    d += 1
            if not absent:
                present.append(v)
        levels.append(d)
    return levels, present


def write_parquet(fields, dst=None, compression='gzip', row_group_size=DEFAULT_ROW_GROUP_SIZE,
                  created_by=CREATED_BY, compression_level=6):
    """Write a Parquet file from ``Column`` / ``Group`` specs.

    ``dst``: None -> return bytes; path -> write file and return the path;
    binary file object -> write into it and return it.
    """
    fields = list(fields)
    leaves, schema_elements = _flatten_columns(fields)
    if not leaves:
        raise ParquetError('no columns')
    nrows = len(leaves[0].column.values)
    for lf in leaves:
        if len(lf.column.values) != nrows:
            raise ParquetError('column %s has %d rows, expected %d' % ('.'.join(lf.path), len(lf.column.values), nrows))
        for g in lf.chain:
            if g.present is not None and len(g.present) != nrows:
                raise ParquetError('group %s presence mask has the wrong length' % g.name)
    codec = CODEC_NAMES[(compression or 'none').lower()]
    out = io.BytesIO()
    out.write(PARQUET_MAGIC)
    row_groups = []
    row_group_size = max(1, int(row_group_size))
    starts = list(range(0, nrows, row_group_size)) or [0]
    for rg_start in starts:
        rg_stop = min(nrows, rg_start + row_group_size)
        rg_rows = rg_stop - rg_start
        chunks = []
        rg_uncompressed = 0
        rg_compressed = 0
        rg_file_offset = out.tell()
        for lf in leaves:
            col = lf.column
            levels, present = _def_levels(lf, rg_start, rg_stop)
            body = bytearray()
            if levels is not None:
                enc = rle_hybrid_encode(levels, _bit_width(lf.max_def))
                body += struct.pack('<I', len(enc))
                body += enc
            body += _plain_encode(col.ptype, present, col.logical)
            body = bytes(body)
            if codec == CODEC_GZIP:
                payload = _gzip_compress(body, compression_level)
            else:
                payload = body
            header = tc.encode_struct([
                (1, 'i32', PAGE_DATA), (2, 'i32', len(body)), (3, 'i32', len(payload)),
                (5, 'struct', [(1, 'i32', rg_rows), (2, 'i32', ENC_PLAIN),
                               (3, 'i32', ENC_RLE), (4, 'i32', ENC_RLE)])])
            page_offset = out.tell()
            out.write(header)
            out.write(payload)
            unc = len(header) + len(body)
            comp = len(header) + len(payload)
            rg_uncompressed += unc
            rg_compressed += comp
            meta = [(1, 'i32', col.ptype), (2, 'list:i32', [ENC_PLAIN, ENC_RLE]),
                    (3, 'list:binary', list(lf.path)), (4, 'i32', codec), (5, 'i64', rg_rows),
                    (6, 'i64', unc), (7, 'i64', comp), (9, 'i64', page_offset)]
            chunks.append([(2, 'i64', 0), (3, 'struct', meta)])
        row_groups.append([(1, 'list:struct', chunks), (2, 'i64', rg_uncompressed),
                           (3, 'i64', rg_rows), (5, 'i64', rg_file_offset),
                           (6, 'i64', rg_compressed), (7, 'i16', len(row_groups))])
    root = [(4, 'binary', 'schema'), (5, 'i32', len(fields))]
    footer = tc.encode_struct([
        (1, 'i32', 2), (2, 'list:struct', [root] + schema_elements), (3, 'i64', nrows),
        (4, 'list:struct', row_groups), (6, 'binary', created_by),
        (7, 'list:struct', [[(1, 'struct', [])] for _ in leaves])])
    out.write(footer)
    out.write(struct.pack('<i', len(footer)))
    out.write(PARQUET_MAGIC)
    data = out.getvalue()
    if dst is None:
        return data
    if hasattr(dst, 'write'):
        dst.write(data)
        return dst
    with open(dst, 'wb') as fh:
        fh.write(data)
    return dst


# ==================================================================== reader
class ParquetFile(object):
    """Decoded Parquet file: ``schema`` (root Node), ``leaves`` (list of leaf
    Nodes in file order), ``num_rows``, ``created_by``, ``key_value``
    (metadata dict) and ``columns`` ({'a.b': [values...]})."""

    def __init__(self, data, lazy=False):
        self.data = data
        self._parse_footer()
        self.columns = {}
        if not lazy:
            self.read_all()

    # -- metadata
    def _parse_footer(self):
        data = self.data
        if len(data) < 12 or data[:4] != PARQUET_MAGIC or data[-4:] != PARQUET_MAGIC:
            raise ParquetError('not a parquet file (missing PAR1 magic)')
        flen = struct.unpack('<i', data[-8:-4])[0]
        if flen <= 0 or flen + 12 > len(data):
            raise ParquetError('bad parquet footer length %d' % flen)
        meta, _ = tc.decode_struct(data[-8 - flen:-8], 0)
        self.version = meta.get(1)
        self.num_rows = meta.get(3, 0)
        self.created_by = _text(meta.get(6, b''))
        self.key_value = {}
        for kv in meta.get(5, []) or []:
            self.key_value[_text(kv.get(1, b''))] = _text(kv.get(2, b'')) if kv.get(2) is not None else None
        elements = meta.get(2) or []
        if not elements:
            raise ParquetError('parquet file has no schema')
        self.schema, nxt = self._build_node(elements, 0)
        if nxt != len(elements):
            raise ParquetError('schema tree does not consume all elements')
        self.leaves = []
        self._assign_paths(self.schema, (), 0, 0, root=True)
        self.row_groups = meta.get(4) or []

    def _build_node(self, elements, idx):
        e = elements[idx]
        name = _text(e.get(4, b''))
        nchildren = e.get(5)
        rep = e.get(3)
        node = Node(name, repetition=rep, field_id=e.get(9))
        if nchildren:
            idx += 1
            for _ in range(nchildren):
                child, idx = self._build_node(elements, idx)
                node.children.append(child)
            return node, idx
        if e.get(1) is None:
            # group with zero children (degenerate) — treat as empty group
            return node, idx + 1
        node.ptype = e[1]
        node.type_length = e.get(2)
        node.converted = e.get(6)
        node.logical = _parse_logical(e.get(10), node.converted)
        return node, idx + 1

    def _assign_paths(self, node, path, max_def, max_rep, root=False):
        if not root:
            path = path + (node.name,)
            if node.repetition == OPTIONAL:
                max_def += 1
            elif node.repetition == REPEATED:
                max_def += 1
                max_rep += 1
        node.path = path
        node.max_def = max_def
        node.max_rep = max_rep
        if node.is_leaf:
            self.leaves.append(node)
        for c in node.children:
            self._assign_paths(c, path, max_def, max_rep)

    def schema_text(self):
        return self.schema.describe()

    def leaf(self, path):
        if isinstance(path, str):
            path = tuple(path.split('.'))
        for lf in self.leaves:
            if lf.path == tuple(path):
                return lf
        return None

    # -- data
    def read_all(self):
        cols = dict((lf.path, []) for lf in self.leaves)
        for rg in self.row_groups:
            chunks = rg.get(1) or []
            if len(chunks) != len(self.leaves):
                raise ParquetError('row group has %d column chunks, schema has %d leaves' % (len(chunks), len(self.leaves)))
            nrows = rg.get(3, 0)
            for lf, chunk in zip(self.leaves, chunks):
                cols[lf.path].extend(self._read_chunk(lf, chunk, nrows))
        self.columns = dict(('.'.join(p), v) for p, v in cols.items())
        return self.columns

    def column(self, path):
        if isinstance(path, (tuple, list)):
            path = '.'.join(path)
        return self.columns[path]

    def _read_chunk(self, leaf, chunk, nrows):
        if chunk.get(1):
            raise ParquetError('external column chunk files are not supported')
        cm = chunk.get(3)
        if cm is None:
            raise ParquetError('column chunk without metadata')
        if leaf.max_rep:
            raise ParquetError('repeated fields (lists/maps) are not supported: %s' % '.'.join(leaf.path))
        codec = cm.get(4, CODEC_UNCOMPRESSED)
        num_values = cm.get(5, 0)
        data_off = cm.get(9)
        dict_off = cm.get(11)
        start = data_off if dict_off is None else min(data_off, dict_off)
        end = start + cm.get(7, 0)
        if start is None or end > len(self.data):
            raise ParquetError('column chunk offsets out of range')
        as_text = leaf.logical is not None and leaf.logical[0] == 'string'
        unsigned_bits = leaf.logical[1] if (leaf.logical and leaf.logical[0] == 'integer' and not leaf.logical[2]) else None
        pos = start
        dictionary = None
        levels_all = []
        values_all = []
        seen = 0
        data = self.data
        while pos < end and seen < num_values:
            header, hpos = tc.decode_struct(data, pos)
            ptype_page = header.get(1)
            comp_size = header.get(3, 0)
            unc_size = header.get(2, 0)
            body = data[hpos:hpos + comp_size]
            pos = hpos + comp_size
            if ptype_page == PAGE_DICTIONARY:
                dh = header.get(7) or {}
                raw = _decompress(codec, body, unc_size)
                n = dh.get(1, 0)
                dictionary, _ = _plain_decode(leaf.ptype, raw, 0, n, leaf.type_length, as_text)
            elif ptype_page == PAGE_DATA:
                dh = header.get(5) or {}
                n = dh.get(1, 0)
                raw = _decompress(codec, body, unc_size)
                p = 0
                if leaf.max_rep:
                    raise ParquetError('repeated fields are not supported')
                if leaf.max_def:
                    levels, p = _decode_levels(raw, p, leaf.max_def, n, dh.get(3, ENC_RLE), v1=True)
                else:
                    levels = None
                nvals = n if levels is None else sum(1 for l in levels if l == leaf.max_def)
                vals = _decode_values(leaf, raw, p, nvals, dh.get(2, ENC_PLAIN), dictionary, as_text)
                levels_all.append((levels, n))
                values_all.append(vals)
                seen += n
            elif ptype_page == PAGE_DATA_V2:
                dh = header.get(8) or {}
                n = dh.get(1, 0)
                nnulls = dh.get(2, 0)
                rl = dh.get(6, 0)
                dl = dh.get(5, 0)
                if rl or leaf.max_rep:
                    raise ParquetError('repeated fields are not supported')
                is_comp = dh.get(7, True)
                levels = None
                if leaf.max_def:
                    levels, _ = rle_hybrid_decode(body, 0, dl, _bit_width(leaf.max_def), n)
                vbytes = body[dl:]
                if is_comp and codec != CODEC_UNCOMPRESSED:
                    vbytes = _decompress(codec, vbytes, unc_size - dl)
                nvals = n - nnulls if levels is None else sum(1 for l in levels if l == leaf.max_def)
                vals = _decode_values(leaf, vbytes, 0, nvals, dh.get(4, ENC_PLAIN), dictionary, as_text)
                levels_all.append((levels, n))
                values_all.append(vals)
                seen += n
            elif ptype_page == PAGE_INDEX:
                continue
            else:
                raise ParquetError('unknown page type %r' % ptype_page)
        out = []
        for (levels, n), vals in zip(levels_all, values_all):
            if levels is None:
                out.extend(vals)
            else:
                it = iter(vals)
                md = leaf.max_def
                out.extend(next(it) if l == md else None for l in levels)
        if unsigned_bits and out:
            wrap = 1 << unsigned_bits
            out = [v if (v is None or v >= 0) else v + wrap for v in out]
        if len(out) != num_values:
            raise ParquetError('column %s: decoded %d values, metadata says %d' % ('.'.join(leaf.path), len(out), num_values))
        return out


def _decode_levels(raw, pos, max_def, count, encoding, v1):
    width = _bit_width(max_def)
    if encoding == ENC_RLE:
        if v1:
            if pos + 4 > len(raw):
                raise ParquetError('truncated level length')
            n = struct.unpack_from('<I', raw, pos)[0]
            pos += 4
            levels, _ = rle_hybrid_decode(raw, pos, pos + n, width, count)
            return levels, pos + n
        return rle_hybrid_decode(raw, pos, len(raw), width, count)
    if encoding == ENC_BIT_PACKED:
        return _bitpacked_deprecated_decode(raw, pos, width, count)
    raise ParquetError('unsupported level encoding %d' % encoding)


def _decode_values(leaf, raw, pos, nvals, encoding, dictionary, as_text):
    if nvals == 0:
        return []
    if encoding == ENC_PLAIN:
        vals, _ = _plain_decode(leaf.ptype, raw, pos, nvals, leaf.type_length, as_text)
        return vals
    if encoding in (ENC_RLE_DICTIONARY, ENC_PLAIN_DICTIONARY):
        if dictionary is None:
            raise ParquetError('dictionary-encoded page without a dictionary page')
        if pos >= len(raw):
            raise ParquetError('truncated dictionary indices')
        width = raw[pos]
        idx, _ = rle_hybrid_decode(raw, pos + 1, len(raw), width, nvals)
        try:
            return [dictionary[i] for i in idx]
        except IndexError:
            raise ParquetError('dictionary index out of range')
    if encoding == ENC_RLE and leaf.ptype == BOOLEAN:
        if pos + 4 > len(raw):
            raise ParquetError('truncated RLE boolean length')
        n = struct.unpack_from('<I', raw, pos)[0]
        bits, _ = rle_hybrid_decode(raw, pos + 4, pos + 4 + n, 1, nvals)
        return [bool(b) for b in bits]
    raise ParquetError('unsupported value encoding %d for column %s' % (encoding, '.'.join(leaf.path)))


def _text(b):
    if isinstance(b, bytes):
        return b.decode('utf-8', 'replace')
    return b


def read_parquet(src):
    """``src``: path, bytes or binary file object -> ParquetFile (all columns
    decoded into ``.columns``)."""
    if isinstance(src, (bytes, bytearray, memoryview)):
        data = bytes(src)
    elif hasattr(src, 'read'):
        data = src.read()
    else:
        with open(src, 'rb') as fh:
            data = fh.read()
    return ParquetFile(data)
