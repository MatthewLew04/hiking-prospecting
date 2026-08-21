#!/usr/bin/env python3
"""geomodel_kit — command line for the 3-D geological model toolkit.

  site      build a model project around a mine / point from the repo bundles
            and export it (OMF v2 + v0.9, Surfer/Geosoft/Arc grids, DXF, OBJ,
            CSV, model3d.html project JSON)
  export    re-export an existing .geomodel.json project
  convert   convert any supported file to another format (see --list)
  info      describe a file (format, objects, bounds, warnings)
  list      list the supported formats

Examples:
  python3 pipelines/geomodel_kit.py site --lon -113.62 --lat 42.17 --name "Silver Hills" --radius 2500
  python3 pipelines/geomodel_kit.py site --grade-index 12 --radius 3000
  python3 pipelines/geomodel_kit.py convert exports/geomodel/silver-hills/topography.grd topo.gxf
  python3 pipelines/geomodel_kit.py convert model.omf model.dxf
  python3 pipelines/geomodel_kit.py info survey.sgy

Everything is stdlib only (numpy speeds up the solvers when present); terrain
tiles are cached under pipelines/cache/terrain/ and --offline never fetches.
"""
import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from geomodel import kit  # noqa: E402
from geomodel.model import Project  # noqa: E402
from geomodel import formats as F  # noqa: E402

ROOT = os.path.normpath(os.path.join(HERE, '..'))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd')

    s = sub.add_parser('site', help='build + export a site model')
    s.add_argument('--lon', type=float)
    s.add_argument('--lat', type=float)
    s.add_argument('--grade-index', type=int, default=None, help='row in site/data/grades/grades.json (sets lon/lat/name)')
    s.add_argument('--name', default=None)
    s.add_argument('--radius', type=float, default=2500.0, help='half-width of the model box, metres')
    s.add_argument('--aoi', default='auto', help='AOI bundle key or "auto" / "none"')
    s.add_argument('--zoom', type=int, default=13, help='terrain tile zoom (13 ~ 15 m at 42N)')
    s.add_argument('--cell', type=float, default=None, help='topography cell size (m)')
    s.add_argument('--out', default=None, help='output dir (default exports/geomodel/<slug>)')
    s.add_argument('--offline', action='store_true')
    s.add_argument('--formats', default='json,omf2,omf1,surfer,asc,gxf,dxf,csv,obj')

    e = sub.add_parser('export', help='export a .geomodel.json project')
    e.add_argument('project')
    e.add_argument('--out', default=None)
    e.add_argument('--formats', default='json,omf2,omf1,surfer,asc,gxf,dxf,csv,obj')

    c = sub.add_parser('convert', help='convert between formats')
    c.add_argument('src')
    c.add_argument('dst')
    c.add_argument('--in-format', default=None)
    c.add_argument('--out-format', default=None)

    i = sub.add_parser('info', help='describe a file')
    i.add_argument('src')
    i.add_argument('--format', default=None)

    sub.add_parser('list', help='list formats')

    args = ap.parse_args(argv)
    if args.cmd == 'site':
        if args.grade_index is None and (args.lon is None or args.lat is None):
            ap.error('site needs --lon/--lat or --grade-index')
        proj = kit.build_site_model(args.lon, args.lat, radius_m=args.radius, name=args.name,
                                    aoi=(None if args.aoi == 'none' else args.aoi),
                                    grade_index=args.grade_index, zoom=args.zoom, cell=args.cell,
                                    offline=args.offline)
        out = args.out or os.path.join(ROOT, 'exports', 'geomodel', kit.slugify(proj.name))
        print('site %s  (%s)  objects: %d  ->  %s' % (proj.name, proj.site.get('utm_zone'), len(proj.objects), out))
        kit.export_project(proj, out, formats=tuple(args.formats.split(',')))
        return 0
    if args.cmd == 'export':
        proj = Project.load(args.project)
        out = args.out or os.path.dirname(os.path.abspath(args.project))
        kit.export_project(proj, out, formats=tuple(args.formats.split(',')))
        return 0
    if args.cmd == 'convert':
        kit.convert(args.src, args.dst, args.in_format, args.out_format)
        return 0
    if args.cmd == 'info':
        print(kit.describe(args.src, args.format))
        return 0
    if args.cmd == 'list':
        for k, (mod, rd, wr, exts, summary) in F.REGISTRY.items():
            print('%-16s %-6s %-22s %s' % (k, 'r' + ('w' if wr else ' '), ' '.join(exts), summary))
        return 0
    ap.print_help()
    return 1


if __name__ == '__main__':
    sys.exit(main())
