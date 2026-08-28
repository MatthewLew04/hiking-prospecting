#!/usr/bin/env python3
"""Generate ``test_v09.omf`` with the reference OMF v0.9 writer (omf 1.0.1).

The geomodel OMF v0.9 reader is validated against a file this project did not
write.  ``omf`` 1.0.1 is the last release of Global Mining Guidelines Group's
original Python implementation and is the normative producer for the format,
so the fixture is generated with it rather than vendored: the bytes stay
reproducible and nobody has to redistribute a binary of unclear provenance.

Run under the reference interpreter, not the repository one::

    $GM_REF_DIR/omfenv/bin/python tools/gen_omf1_reference.py $GM_REF_DIR/test_v09.omf

``tools/fetch_gm_refs.py`` does this for you.  The element/attribute inventory
below is what ``tests/test_geomodel_omf.py`` asserts on; keep them in step.
"""

import os
import struct
import sys
import zlib

import omf


def _png(path):
    """A 1x1 opaque-red PNG, written by hand so the generator needs no
    imaging dependency.  Only its presence matters: the reader must report
    the texture as skipped, because OMF v0.9 image textures have no v2
    equivalent."""
    def chunk(tag, payload):
        body = tag + payload
        return struct.pack('>I', len(payload)) + body + struct.pack('>I', zlib.crc32(body) & 0xffffffff)

    ihdr = struct.pack('>IIBBBBB', 1, 1, 8, 2, 0, 0, 0)
    raw = zlib.compress(b'\x00\xff\x00\x00')
    with open(path, 'wb') as fh:
        fh.write(b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', ihdr) + chunk(b'IDAT', raw) + chunk(b'IEND', b''))
    return path


def build(png_path):
    gradient = [[i * 2, 255 - i * 2, 128] for i in range(128)]

    points = omf.PointSetElement(
        name='pts',
        subtype='point',
        color=[255, 0, 0],
        geometry=omf.PointSetGeometry(
            origin=[1.0, 2.0, 3.0],
            vertices=[[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0]],
        ),
        data=[
            omf.ScalarData(name='scalar', location='vertices', array=[1.5, 2.5, 3.5],
                           colormap=omf.ScalarColormap(limits=[0.0, 10.0], gradient=gradient)),
            omf.StringData(name='strings', location='vertices', array=['a', 'b', 'c']),
            omf.Vector3Data(name='vec3', location='vertices',
                            array=[[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]),
            omf.Vector2Data(name='vec2', location='vertices',
                            array=[[1.0, 0.0], [0.0, 1.0], [0.0, 0.0]]),
            omf.ColorData(name='colors', location='vertices',
                          array=[[255, 0, 0], [0, 255, 0], [0, 0, 255]]),
            omf.DateTimeData(name='dates', location='vertices',
                             array=['2020-01-01T00:00:00Z', '2020-01-02T00:00:00Z', '2020-01-03T00:00:00Z']),
            omf.MappedData(name='mapped', location='vertices', array=[0, 1, -1],
                           legends=[omf.Legend(name='', values=omf.StringArray(array=['x', 'y']))]),
        ],
    )

    lines = omf.LineSetElement(
        name='lines',
        subtype='borehole',
        geometry=omf.LineSetGeometry(
            vertices=[[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [2.0, 0.0, 0.0]],
            segments=[[0, 1], [1, 2]],
        ),
        data=[omf.ScalarData(name='segdata', location='segments', array=[1.0, 2.0])],
    )

    surf = omf.SurfaceElement(
        name='surf',
        geometry=omf.SurfaceGeometry(
            vertices=[[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0]],
            triangles=[[0, 1, 2]],
        ),
        data=[omf.ScalarData(name='facedata', location='faces', array=[7.0])],
        textures=[omf.ImageTexture(name='tex', origin=[0.0, 0.0, 0.0],
                                   axis_u=[1.0, 0.0, 0.0], axis_v=[0.0, 1.0, 0.0],
                                   image=png_path)],
    )

    gridsurf = omf.SurfaceElement(
        name='gridsurf',
        geometry=omf.SurfaceGridGeometry(
            origin=[0.0, 0.0, 0.0],
            axis_u=[1.0, 0.0, 0.0], axis_v=[0.0, 1.0, 0.0],
            tensor_u=[1.0, 1.0], tensor_v=[2.0, 2.0],
        ),
    )

    vol = omf.VolumeElement(
        name='vol',
        geometry=omf.VolumeGridGeometry(
            origin=[10.0, 20.0, 30.0],
            axis_u=[1.0, 0.0, 0.0], axis_v=[0.0, 1.0, 0.0], axis_w=[0.0, 0.0, 1.0],
            tensor_u=[1.0, 1.0], tensor_v=[1.0], tensor_w=[1.0],
        ),
        data=[omf.ScalarData(name='celldata', location='cells', array=[1.0, 2.0])],
    )

    return omf.Project(
        name='Test Project',
        author='me',
        revision='r1',
        origin=[100.0, 200.0, 300.0],
        elements=[points, lines, surf, gridsurf, vol],
    )


def main(argv):
    if len(argv) != 2:
        print(__doc__.strip().splitlines()[0], file=sys.stderr)
        print('usage: gen_omf1_reference.py <out.omf>', file=sys.stderr)
        return 2
    out = argv[1]
    outdir = os.path.dirname(os.path.abspath(out))
    if outdir:
        os.makedirs(outdir, exist_ok=True)
    png = _png(os.path.join(outdir, '_tex.png'))
    try:
        project = build(png)
        assert project.validate()
        if os.path.exists(out):
            os.remove(out)
        omf.OMFWriter(project, out)
    finally:
        if os.path.exists(png):
            os.remove(png)
    print('wrote %s (%d bytes)' % (out, os.path.getsize(out)))
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv))
