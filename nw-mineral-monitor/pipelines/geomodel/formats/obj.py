"""Wavefront OBJ triangle meshes (read + write), stdlib only.

Reader (``read_obj``) understands the geometry subset every modelling
package exports -- ``v`` (with optional w / vertex colours), ``f`` with the
``v``, ``v/vt``, ``v//vn`` and ``v/vt/vn`` index forms, 1-based and negative
(relative) indices, polygons with more than three corners (fan-triangulated),
``\\`` line continuations, ``o`` / ``g`` groups (merged into one Mesh; the
group of each face is kept as a per-face ``group`` attribute indexing
``metadata['groups']``), and ``l`` polylines (counted in
``metadata['lines']`` only).  ``vt`` / ``vn`` / materials are skipped.

Writer (``write_obj``) emits triangles only, 1-based, with a comment header
and optional per-vertex normals (``normals=True``).
"""
import io
import math
import os
import re

from ..model import Mesh, farray, iarray

FORMAT_ID = 'obj'


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
    """Write bytes to a path or a binary file object; return the path (or the
    bytes when a BytesIO was given)."""
    if hasattr(dst, 'write'):
        dst.write(data)
        if isinstance(dst, io.BytesIO):
            return dst.getvalue()
        return getattr(dst, 'name', None)
    with open(dst, 'wb') as fh:
        fh.write(data)
    return os.fspath(dst)


def _decode(data):
    for enc in ('utf-8', 'cp1252', 'latin-1'):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode('latin-1', 'replace')


def _fmt(v):
    v = float(v)
    if v != v or v in (math.inf, -math.inf):
        return '0'
    r = repr(v)
    if r.endswith('.0'):
        r = r[:-2]
    return r


def _logical_lines(text):
    """Yield (line_number, line) with backslash continuations joined."""
    buf = None
    start = 0
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.rstrip()
        if buf is None:
            start = n
            buf = ''
        if line.endswith('\\'):
            buf += line[:-1] + ' '
            continue
        buf += line
        yield start, buf
        buf = None
    if buf:
        yield start, buf


# ------------------------------------------------------------------- reader
def read_obj(src, name=None):
    """Parse a Wavefront OBJ file (path / bytes / file object) into a Mesh.

    Faces with more than three corners are fan-triangulated; all objects and
    groups are merged (``metadata['groups']`` lists them, and when there is
    more than one, the per-face attribute ``group`` holds the index).
    Polylines (``l``) are not kept -- ``metadata['lines']`` counts them.
    """
    path = _src_path(src)
    text = _decode(_load_bytes(src))
    verts = farray()
    tris = iarray()
    warnings = []
    groups = []          # ordered group / object names
    face_group = []      # per-triangle group index
    cur_group = None
    n_lines = 0
    n_vt = n_vn = 0
    n_faces = 0
    n_quads = 0
    bad_faces = 0
    materials = []
    mtllibs = []
    obj_name = None

    def group_index(gname):
        if gname not in groups:
            groups.append(gname)
        return groups.index(gname)

    for lineno, line in _logical_lines(text):
        s = line.strip()
        if not s or s[0] == '#':
            continue
        parts = s.split()
        key = parts[0]
        if key == 'v':
            if len(parts) < 4:
                warnings.append('line %d: vertex with < 3 coordinates skipped' % lineno)
                verts.extend((0.0, 0.0, 0.0))
                continue
            try:
                verts.extend((float(parts[1]), float(parts[2]), float(parts[3])))
            except ValueError:
                warnings.append('line %d: unparsable vertex %r' % (lineno, s[:40]))
                verts.extend((0.0, 0.0, 0.0))
        elif key == 'f':
            nv = len(verts) // 3
            idx = []
            ok = True
            for tok in parts[1:]:
                vi = tok.split('/')[0]
                try:
                    i = int(vi)
                except ValueError:
                    ok = False
                    break
                if i < 0:
                    i = nv + i          # relative to the vertices seen so far
                else:
                    i = i - 1
                if i < 0 or i >= nv:
                    ok = False
                    break
                idx.append(i)
            if not ok or len(idx) < 3:
                bad_faces += 1
                continue
            n_faces += 1
            if len(idx) > 3:
                n_quads += 1
            gi = group_index(cur_group) if cur_group is not None else None
            for k in range(1, len(idx) - 1):
                tris.extend((idx[0], idx[k], idx[k + 1]))
                face_group.append(gi)
        elif key == 'vt':
            n_vt += 1
        elif key == 'vn':
            n_vn += 1
        elif key == 'l':
            n_lines += 1
        elif key in ('o', 'g'):
            gname = ' '.join(parts[1:]) if len(parts) > 1 else ''
            if key == 'o' and obj_name is None and gname:
                obj_name = gname
            cur_group = gname
            group_index(gname)
        elif key == 'usemtl':
            m = ' '.join(parts[1:])
            if m not in materials:
                materials.append(m)
        elif key == 'mtllib':
            mtllibs.append(' '.join(parts[1:]))
        # vp, s, curv, surf ... ignored

    if bad_faces:
        warnings.append('%d face(s) with invalid or out-of-range vertex indices were skipped' % bad_faces)
    if n_lines:
        warnings.append('%d polyline (l) record(s) ignored (counted in metadata["lines"])' % n_lines)
    if not len(tris) and len(verts):
        warnings.append('no faces: file holds %d vertices only' % (len(verts) // 3))

    mesh_name = name or obj_name or (groups[0] if len(groups) == 1 and groups[0] else None)
    if not mesh_name:
        mesh_name = os.path.splitext(os.path.basename(path))[0] if path else 'obj mesh'
    mesh = Mesh(verts, tris, name=mesh_name,
                provenance={'format': FORMAT_ID, 'path': path})
    if len(groups) > 1 and any(g is not None for g in face_group):
        mesh.attributes['group'] = {'location': 'faces',
                                    'values': farray(-1 if g is None else g for g in face_group)}
    md = mesh.metadata
    md['groups'] = list(groups)
    md['lines'] = n_lines
    md['faces'] = n_faces
    md['polygons_triangulated'] = n_quads
    md['texcoords'] = n_vt
    md['normals'] = n_vn
    if materials:
        md['materials'] = materials
    if mtllibs:
        md['mtllib'] = mtllibs
    md['warnings'] = warnings
    return mesh


# ------------------------------------------------------------------- writer
def _vertex_normals(mesh):
    n = mesh.n_vertices
    nx = [0.0] * n
    ny = [0.0] * n
    nz = [0.0] * n
    v = mesh.vertices
    t = mesh.triangles
    for k in range(0, len(t) - 2, 3):
        a, b, c = t[k], t[k + 1], t[k + 2]
        ax, ay, az = v[3 * a], v[3 * a + 1], v[3 * a + 2]
        ux, uy, uz = v[3 * b] - ax, v[3 * b + 1] - ay, v[3 * b + 2] - az
        wx, wy, wz = v[3 * c] - ax, v[3 * c + 1] - ay, v[3 * c + 2] - az
        cx, cy, cz = uy * wz - uz * wy, uz * wx - ux * wz, ux * wy - uy * wx   # area-weighted
        for i in (a, b, c):
            nx[i] += cx
            ny[i] += cy
            nz[i] += cz
    out = []
    for i in range(n):
        ln = math.sqrt(nx[i] ** 2 + ny[i] ** 2 + nz[i] ** 2)
        if ln > 0:
            out.append((nx[i] / ln, ny[i] / ln, nz[i] / ln))
        else:
            out.append((0.0, 0.0, 1.0))
    return out


def write_obj(mesh, dst, name=None, normals=False, comment=None):
    """Write a Mesh as Wavefront OBJ (triangles only, 1-based indices).

    ``normals=True`` adds area-weighted per-vertex ``vn`` records and writes
    faces as ``v//vn``.  Returns the path written (bytes for a BytesIO).
    """
    oname = name or mesh.name or 'mesh'
    oname = re.sub(r'\s+', '_', oname.strip()) or 'mesh'
    out = []
    w = out.append
    w('# Wavefront OBJ written by nw-mineral-monitor geomodel')
    if comment:
        for c in str(comment).splitlines():
            w('# ' + c)
    w('# %d vertices, %d triangles' % (mesh.n_vertices, mesh.n_triangles))
    w('o ' + oname)
    v = mesh.vertices
    for i in range(0, len(v) - 2, 3):
        w('v %s %s %s' % (_fmt(v[i]), _fmt(v[i + 1]), _fmt(v[i + 2])))
    if normals:
        for nx, ny, nz in _vertex_normals(mesh):
            w('vn %s %s %s' % (_fmt(nx), _fmt(ny), _fmt(nz)))
    t = mesh.triangles
    if normals:
        for k in range(0, len(t) - 2, 3):
            a, b, c = t[k] + 1, t[k + 1] + 1, t[k + 2] + 1
            w('f %d//%d %d//%d %d//%d' % (a, a, b, b, c, c))
    else:
        for k in range(0, len(t) - 2, 3):
            w('f %d %d %d' % (t[k] + 1, t[k + 1] + 1, t[k + 2] + 1))
    data = ('\n'.join(out) + '\n').encode('utf-8')
    return _emit(dst, data)
