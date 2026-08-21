"""geomodel.formats — interchange readers/writers.

Every module exposes plain functions (no classes to instantiate):

    read_<fmt>(path_or_bytes, **opts)  -> model object (Grid2D / Mesh /
                                           LineSet / PointSet / BlockModel /
                                           Drillholes / dict for tables)
    write_<fmt>(obj, path_or_fileobj, **opts) -> the path written (or bytes
                                           when a BytesIO was given)

Readers accept a filesystem path (str) or raw ``bytes``; writers accept a path
or a binary file object.  Readers never guess silently: anything unusual in a
file lands in ``obj.metadata['warnings']`` (a list of strings).

``REGISTRY`` maps a format id to (module, reader, writer, extensions, summary)
so the CLI and the kit builder can route by extension; ``detect(path, head)``
sniffs magic bytes where a format has them.
"""
import importlib
import os

REGISTRY = {
    # id              module       reader            writer            extensions                summary
    'surfer_grd':    ('surfer',    'read_grd',       'write_grd',      ('.grd',),                 'Golden Software Surfer grid (DSAA ascii / DSBB Surfer 6 / DSRB Surfer 7)'),
    'surfer_bln':    ('surfer',    'read_bln',       'write_bln',      ('.bln',),                 'Surfer blanking / breakline polylines'),
    'geosoft_grd':   ('geosoft',   'read_grd',       'write_grd',      ('.grd',),                 'Geosoft Oasis montaj binary grid (v2, uncompressed + compressed read)'),
    'gxf':           ('geosoft',   'read_gxf',       'write_gxf',      ('.gxf',),                 'Geosoft Grid eXchange File (ASCII, incl. base-90 compressed read)'),
    'geosoft_xyz':   ('geosoft',   'read_xyz',       'write_xyz',      ('.xyz',),                 'Geosoft XYZ line database export (channels + Line/Tie headers)'),
    'arc_ascii':     ('arcascii',  'read_asc',       'write_asc',      ('.asc', '.txt'),          'Arc/Info ASCII grid'),
    'zmap':          ('zmap',      'read_zmap',      'write_zmap',     ('.zmap', '.dat', '.zmp'), 'ZMAP+ ASCII grid (Kingdom / Petrel / Landmark)'),
    'irap':          ('irap',      'read_irap',      'write_irap',     ('.irap', '.gri'),         'Irap classic ASCII grid (RMS / Petrel)'),
    'cps3':          ('cps3',      'read_cps3',      None,             ('.cps3', '.cps'),         'CPS-3 ASCII grid (read; column direction flagged)'),
    'ubc':           ('ubc',       'read_ubc',       'write_ubc',      ('.msh',),                 'UBC-GIF 3-D mesh + model files (Geosoft / Leapfrog voxel interchange)'),
    'omf1':          ('omf1',      'read_omf1',      'write_omf1',     ('.omf',),                 'Open Mining Format v0.9 (Leapfrog Geo <= 2024.1)'),
    'omf2':          ('omf2',      'read_omf2',      'write_omf2',     ('.omf',),                 'Open Mining Format v2.0 (Leapfrog Geo 2025.1+, Seequent Evo)'),
    'obj':           ('obj',       'read_obj',       'write_obj',      ('.obj',),                 'Wavefront OBJ mesh'),
    'dxf':           ('dxf',       'read_dxf',       'write_dxf',      ('.dxf',),                 'AutoCAD DXF R12 (3DFACE meshes, POLYLINE 3-D polylines, POINT)'),
    'gocad_ts':      ('gocad',     'read_gocad',     'write_gocad',    ('.ts', '.pl', '.vs'),     'GOCAD TSurf / PLine / VSet ASCII'),
    'lf_msh':        ('lfmsh',     'read_msh',       'write_msh',      ('.msh',),                 'Leapfrog binary mesh (.msh, community-documented layout)'),
    'csv_points':    ('tables',    'read_points_csv', 'write_points_csv', ('.csv',),              'Point table CSV (x,y,z + columns; Leapfrog Points import)'),
    'csv_drillholes': ('tables',   'read_drillholes', 'write_drillholes', ('.csv',),              'Drillhole collar / survey / interval CSV set'),
    'csv_structural': ('tables',   'read_structural_csv', 'write_structural_csv', ('.csv',),      'Planar structural data CSV (x,y,z,dip,dip_azimuth,polarity)'),
    'csv_blockmodel': ('tables',   'read_blockmodel_csv', 'write_blockmodel_csv', ('.csv',),      'Block-model CSV (centroids + sizes; Leapfrog import/export style)'),
    'segy':          ('segy',      'read_segy',      'write_segy',     ('.sgy', '.segy'),         'SEG-Y rev 0/1 seismic / GPR / resistivity section'),
    'las':           ('las',       'read_las',       'write_las',      ('.las',),                 'CWLS LAS 2.0 well log'),
}


def module(fmt):
    return importlib.import_module(__name__ + '.' + REGISTRY[fmt][0])


def reader(fmt):
    m = module(fmt)
    return getattr(m, REGISTRY[fmt][1])


def writer(fmt):
    m = module(fmt)
    name = REGISTRY[fmt][2]
    return getattr(m, name) if name else None


def formats_for_extension(ext):
    ext = ext.lower()
    return [k for k, v in REGISTRY.items() if ext in v[3]]


def sniff(path=None, head=None):
    """Best-effort format id from magic bytes / extension. ``head`` = first
    few hundred bytes when already read."""
    if head is None and path:
        with open(path, 'rb') as fh:
            head = fh.read(1024)
    head = head or b''
    ext = os.path.splitext(path or '')[1].lower()
    if head[:4] == b'\x84\x83\x82\x81':
        return 'omf1'
    if head[:2] == b'PK' and ext == '.omf':
        return 'omf2'
    if head[:4] in (b'DSAA', b'DSBB', b'DSRB'):
        return 'surfer_grd'
    if head[:12].startswith(b'%%ARANZ'):
        return 'lf_msh'
    if head.lstrip()[:5] == b'GOCAD':
        return 'gocad_ts'
    if ext == '.gxf' or head.lstrip().startswith(b'#TITLE') or head.lstrip().startswith(b'#POINTS'):
        return 'gxf'
    if ext in ('.sgy', '.segy'):
        return 'segy'
    if ext == '.las' or head.lstrip()[:2] == b'~V':
        return 'las'
    if ext == '.dxf' or head.lstrip()[:9] in (b'0\r\nSECTIO', b'0\nSECTION'):
        return 'dxf'
    if ext == '.obj':
        return 'obj'
    if ext == '.grd':
        # Geosoft binary grid: first int32 element size 1/2/4/8 (or +1024)
        if len(head) >= 8:
            import struct
            es = struct.unpack('<i', head[:4])[0]
            if es in (1, 2, 4, 8, 1025, 1026, 1028, 1032):
                return 'geosoft_grd'
        return 'surfer_grd'
    if ext == '.asc':
        txt = head.lstrip().lower()
        if txt.startswith(b'ncols') or txt.startswith(b'nrows') or txt.startswith(b'xllcorner'):
            return 'arc_ascii'
    if ext in ('.zmap', '.zmp') or head.lstrip()[:1] in (b'!', b'@'):
        if b'@' in head:
            return 'zmap'
    if head.lstrip()[:4] == b'-996':
        return 'irap'
    if head.lstrip()[:6] == b'FSASCI':
        return 'cps3'
    if ext == '.msh':
        return 'ubc'
    if ext == '.xyz':
        return 'geosoft_xyz'
    if ext == '.csv':
        return 'csv_points'
    return None
