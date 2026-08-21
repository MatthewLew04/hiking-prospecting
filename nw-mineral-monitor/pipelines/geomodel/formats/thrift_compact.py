"""Thrift *compact* protocol — the subset Apache Parquet uses for its file
footer (``FileMetaData``) and page headers.

Parquet stores its metadata as Thrift structs encoded with the compact
protocol.  Only a handful of constructs appear: structs, i8/i16/i32/i64
(zig-zag varints), doubles, binary/strings, lists and bools (which are folded
into the field header).  This module implements exactly that — no maps, no
sets, no service calls.

Decoding is schema-less: a struct becomes ``{field_id: value}``; lists become
Python lists; binary fields stay ``bytes`` (callers decode the strings they
know about); bools are Python bools; ints and doubles are ints and floats.

Encoding takes an explicit field list so the writer controls the wire type:

    encode_struct([(1, 'i32', 2), (4, 'binary', b'x'), (9, 'list:struct', [...])])

Kinds: ``bool``, ``byte``, ``i16``, ``i32``, ``i64``, ``double``, ``binary``
(``bytes`` or ``str`` -> UTF-8), ``struct`` (value = field list) and
``list:<kind>`` (value = list of items of that kind).

Reference: https://github.com/apache/thrift/blob/master/doc/specs/thrift-compact-protocol.md
"""
import struct

# wire type codes (compact protocol)
T_BOOL_TRUE = 1
T_BOOL_FALSE = 2
T_BYTE = 3
T_I16 = 4
T_I32 = 5
T_I64 = 6
T_DOUBLE = 7
T_BINARY = 8
T_LIST = 9
T_SET = 10
T_MAP = 11
T_STRUCT = 12

_KIND_TO_TYPE = {'bool': T_BOOL_TRUE, 'byte': T_BYTE, 'i16': T_I16, 'i32': T_I32,
                 'i64': T_I64, 'double': T_DOUBLE, 'binary': T_BINARY,
                 'struct': T_STRUCT, 'list': T_LIST}


class ThriftError(ValueError):
    """Malformed compact-protocol data."""


# ------------------------------------------------------------------ varints
def encode_varint(n):
    """Unsigned LEB128 varint."""
    if n < 0:
        raise ValueError('varint must be unsigned')
    out = bytearray()
    while True:
        b = n & 0x7f
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def decode_varint(buf, pos):
    """-> (value, new_pos)."""
    shift = 0
    out = 0
    while True:
        if pos >= len(buf):
            raise ThriftError('truncated varint')
        b = buf[pos]
        pos += 1
        out |= (b & 0x7f) << shift
        if not b & 0x80:
            return out, pos
        shift += 7
        if shift > 70:
            raise ThriftError('varint too long')


def zigzag_encode(n):
    return (n << 1) ^ (n >> 63) if n < 0 else n << 1


def zigzag_decode(n):
    return (n >> 1) ^ -(n & 1)


# ------------------------------------------------------------------ decoding
def decode_struct(buf, pos=0):
    """Decode one struct starting at ``pos``; -> (dict, new_pos)."""
    out = {}
    last_id = 0
    while True:
        if pos >= len(buf):
            raise ThriftError('truncated struct')
        header = buf[pos]
        pos += 1
        if header == 0:
            return out, pos
        delta = header >> 4
        wtype = header & 0x0f
        if delta:
            fid = last_id + delta
        else:
            z, pos = decode_varint(buf, pos)
            fid = zigzag_decode(z)
        last_id = fid
        value, pos = _decode_value(buf, pos, wtype)
        out[fid] = value


def _decode_value(buf, pos, wtype):
    if wtype == T_BOOL_TRUE:
        return True, pos
    if wtype == T_BOOL_FALSE:
        return False, pos
    if wtype == T_BYTE:
        if pos >= len(buf):
            raise ThriftError('truncated byte')
        return struct.unpack_from('<b', buf, pos)[0], pos + 1
    if wtype in (T_I16, T_I32, T_I64):
        z, pos = decode_varint(buf, pos)
        return zigzag_decode(z), pos
    if wtype == T_DOUBLE:
        if pos + 8 > len(buf):
            raise ThriftError('truncated double')
        return struct.unpack_from('<d', buf, pos)[0], pos + 8
    if wtype == T_BINARY:
        n, pos = decode_varint(buf, pos)
        if pos + n > len(buf):
            raise ThriftError('truncated binary')
        return bytes(buf[pos:pos + n]), pos + n
    if wtype in (T_LIST, T_SET):
        if pos >= len(buf):
            raise ThriftError('truncated list header')
        header = buf[pos]
        pos += 1
        n = header >> 4
        etype = header & 0x0f
        if n == 15:
            n, pos = decode_varint(buf, pos)
        items = []
        for _ in range(n):
            if etype in (T_BOOL_TRUE, T_BOOL_FALSE):
                # bools inside collections are a single byte (1 = true)
                items.append(buf[pos] == 1)
                pos += 1
            else:
                v, pos = _decode_value(buf, pos, etype)
                items.append(v)
        return items, pos
    if wtype == T_MAP:
        if pos >= len(buf):
            raise ThriftError('truncated map header')
        n, pos = decode_varint(buf, pos)
        if n == 0:
            return {}, pos
        kv = buf[pos]
        pos += 1
        ktype, vtype = kv >> 4, kv & 0x0f
        out = {}
        for _ in range(n):
            k, pos = _decode_value(buf, pos, ktype)
            v, pos = _decode_value(buf, pos, vtype)
            out[k] = v
        return out, pos
    if wtype == T_STRUCT:
        return decode_struct(buf, pos)
    raise ThriftError('unknown compact type %d' % wtype)


# ------------------------------------------------------------------ encoding
def encode_struct(fields):
    """Encode ``[(field_id, kind, value), ...]`` (sorted by id) -> bytes.
    A value of ``None`` skips the field (optional-field semantics)."""
    out = bytearray()
    last_id = 0
    for fid, kind, value in sorted(fields, key=lambda f: f[0]):
        if value is None:
            continue
        base = kind.split(':', 1)[0]
        if base == 'bool':
            wtype = T_BOOL_TRUE if value else T_BOOL_FALSE
        else:
            wtype = _KIND_TO_TYPE[base]
        delta = fid - last_id
        if 0 < delta < 16:
            out.append((delta << 4) | wtype)
        else:
            out.append(wtype)
            out += encode_varint(zigzag_encode(fid))
        last_id = fid
        if base != 'bool':
            out += _encode_value(kind, value)
    out.append(0)
    return bytes(out)


def _encode_value(kind, value):
    base, _, sub = kind.partition(':')
    if base == 'byte':
        return struct.pack('<b', value)
    if base in ('i16', 'i32', 'i64'):
        return encode_varint(zigzag_encode(int(value)))
    if base == 'double':
        return struct.pack('<d', value)
    if base == 'binary':
        if isinstance(value, str):
            value = value.encode('utf-8')
        return encode_varint(len(value)) + bytes(value)
    if base == 'struct':
        return encode_struct(value)
    if base == 'list':
        if not sub:
            raise ValueError('list kind needs an element kind, e.g. list:i32')
        esub = sub.split(':', 1)[0]
        etype = _KIND_TO_TYPE[esub]
        n = len(value)
        out = bytearray()
        if n < 15:
            out.append((n << 4) | etype)
        else:
            out.append(0xf0 | etype)
            out += encode_varint(n)
        for item in value:
            if esub == 'bool':
                out.append(1 if item else 2)
            else:
                out += _encode_value(sub, item)
        return bytes(out)
    raise ValueError('unknown thrift kind %r' % kind)
