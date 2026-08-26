#!/usr/bin/env python3
"""geomodel_kit — command line for the 3-D geological model toolkit.

  site      build a model project around a mine / point from the repo bundles
            and export it (OMF v2 + v0.9, Surfer/Geosoft/Arc grids, DXF, OBJ,
            CSV, model3d.html project JSON)
  mines     search the grades bundle for a mine by name -> candidates
  narrate   parse a written mine description -> elements + the questions it
            leaves open; with --mine-id, build and export the 3-D model too
  export    re-export an existing .geomodel.json project
  convert   convert any supported file to another format (see --list)
  info      describe a file (format, objects, bounds, warnings)
  list      list the supported formats

Examples:
  python3 pipelines/geomodel_kit.py site --lon -113.62 --lat 42.17 --name "Silver Hills" --radius 2500
  python3 pipelines/geomodel_kit.py site --grade-index 12 --radius 3000
  python3 pipelines/geomodel_kit.py mines "White Caps" --state NV
  python3 pipelines/geomodel_kit.py narrate --text "An adit driven N45E for 900 feet."
  python3 pipelines/geomodel_kit.py narrate --file desc.txt --mine-id grades:17 --out build/
  python3 pipelines/geomodel_kit.py convert exports/geomodel/silver-hills/topography.grd topo.gxf
  python3 pipelines/geomodel_kit.py convert model.omf model.dxf
  python3 pipelines/geomodel_kit.py info survey.sgy

Everything is stdlib only (numpy speeds up the solvers when present); terrain
tiles are cached under pipelines/cache/terrain/ and --offline never fetches.
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from geomodel import kit  # noqa: E402
from geomodel import agentbuild, narrative, render2d, resolve  # noqa: E402
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

    m = sub.add_parser('mines', help='find a mine in the grades bundle')
    m.add_argument('name')
    m.add_argument('--state', default=None)
    m.add_argument('--district', default=None)
    m.add_argument('--county', default=None)
    m.add_argument('--limit', type=int, default=8)
    m.add_argument('--json', action='store_true')

    n = sub.add_parser('narrate', help='parse a description; optionally build the model')
    src = n.add_mutually_exclusive_group(required=True)
    src.add_argument('--text', help='the description itself')
    src.add_argument('--file', help='a file holding the description ("-" for stdin)')
    n.add_argument('--mine-id', default=None, help='grades:<n> from the "mines" command; builds the model')
    n.add_argument('--answers', default=None, help='JSON file: [{"id":"g1","value":45.0,"because":"..."}]')
    n.add_argument('--out', default=None, help='write the model + views here')
    n.add_argument('--context', action='store_true', help='include terrain, geology and grade points')
    n.add_argument('--radius', type=float, default=1200.0)
    n.add_argument('--zoom', type=int, default=13)
    n.add_argument('--offline', action='store_true')
    n.add_argument('--json', action='store_true', help='print the tool-shaped JSON and nothing else')

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
    if args.cmd == 'mines':
        return cmd_mines(args)
    if args.cmd == 'narrate':
        return cmd_narrate(ap, args)
    if args.cmd == 'list':
        for k, (mod, rd, wr, exts, summary) in F.REGISTRY.items():
            print('%-16s %-6s %-22s %s' % (k, 'r' + ('w' if wr else ' '), ' '.join(exts), summary))
        return 0
    ap.print_help()
    return 1


def cmd_mines(args):
    got = resolve.lookup(args.name, args.state, args.district, args.county, args.limit)
    if args.json:
        print(json.dumps(got, indent=2))
        return 0
    if not got['candidates']:
        print('no mine in the grades bundle matches %r' % args.name)
        return 1
    print('%d candidate(s) for %r%s' % (len(got['candidates']), args.name,
                                        '  [AMBIGUOUS - choose one]' if got['ambiguous'] else ''))
    for c in got['candidates']:
        where = ', '.join(x for x in (c['state'], c['district'], c['county']) if x)
        print('  %-14s %-52s %-28s %s' % (c['mine_id'], (c['name'] or '')[:52], where[:28],
                                          '' if c['located'] else '(no coordinate on file)'))
        if c['source_url']:
            print('  %-14s %s' % ('', c['source_url']))
    return 0


def cmd_narrate(ap, args):
    text = sys.stdin.read() if args.file == '-' else (
        args.text if args.text is not None else open(args.file, encoding='utf-8').read())
    spec = narrative.parse(text, mine_id=args.mine_id)
    if args.answers:
        with open(args.answers, encoding='utf-8') as fh:
            spec = narrative.apply_answers(spec, json.load(fh))

    if args.mine_id is None:
        out = {'spec_id': spec['spec_id'], 'elements': spec['elements'],
               'gaps': spec['gaps'], 'coverage': spec['coverage']}
        if args.json:
            print(json.dumps(out, indent=2))
        else:
            _print_spec(spec)
        return 0

    site = resolve.site(args.mine_id, zoom=args.zoom, offline=args.offline)
    try:
        built = agentbuild.build(spec, site, context=args.context, radius_m=args.radius,
                                 zoom=args.zoom, offline=args.offline,
                                 log=(lambda *a: None) if args.json else print)
    except agentbuild.Unplaceable as exc:
        print(json.dumps({'error': 'unplaceable', 'detail': str(exc)}, indent=2))
        return 1

    out_dir = args.out or os.path.join(ROOT, 'exports', 'geomodel',
                                       kit.slugify(site.get('name') or args.mine_id))
    manifest = agentbuild.write_exports(built, out_dir)
    views = render2d.render(built)
    for name, svg in sorted(views.items()):
        with open(os.path.join(out_dir, name + '.svg'), 'w', encoding='utf-8') as fh:
            fh.write(svg)
        manifest.append((name + '.svg', '%s view' % name))

    result = {'spec_id': spec['spec_id'], 'mine': {k: site.get(k) for k in
              ('mine_id', 'name', 'state', 'district', 'lon', 'lat', 'elevation_m',
               'elevation_source', 'source', 'source_url')},
              'out_dir': out_dir, 'files': [n for n, _ in manifest],
              'confidence': built['confidence'], 'levels': built['levels'],
              'summary': built['summary'], 'warnings': built['warnings'],
              'unresolved': narrative.unresolved(spec) + built['gaps'],
              'coverage': spec['coverage'],
              'content_sha256': agentbuild.content_sha256(built['project'])}
    if args.json:
        print(json.dumps(result, indent=2, default=str))
        return 0
    _print_spec(spec)
    print()
    print('built %d of %d elements at %s (%s)' % (len(built['placed']), len(spec['elements']),
                                                  site.get('name'), args.mine_id))
    for rec in built['placed']:
        print('  %-9s %-16s %s' % (rec['kind'], (rec['name'] or '')[:16], rec['placement']))
    for w in built['warnings']:
        print('  WARNING: %s' % w)
    for g in built['gaps']:
        print('  UNPLACED %s: %s' % (g['element'], g['question']))
    print()
    print('%-26s %s' % ('confidence', built['confidence']))
    for f, desc in manifest:
        print('  %-26s %s' % (f, desc))
    print('-> %s' % out_dir)
    return 0


def _print_spec(spec):
    cov = spec['coverage']
    print('%d element(s), %d question(s) (%d must be answered) from %d sentence(s); '
          '%d mining sentence(s) understood, %d not'
          % (len(spec['elements']), len(spec['gaps']), cov['unresolved'], cov['sentences'],
             cov['sentences_with_elements'], cov['unparsed_sentences']))
    for el in spec['elements']:
        bits = []
        for f in ('bearing_deg', 'length_m', 'depth_m', 'height_m', 'dip_deg', 'level'):
            if el.get(f) is not None:
                bits.append('%s=%s' % (f, el[f]))
        print('  %-4s %-9s %-14s %-11s %s' % (el['id'], el['kind'], (el.get('name') or '')[:14],
                                              el['confidence'], '  '.join(bits)))
        print('       "%s"' % el['quote'][:96])
    for g in spec['gaps']:
        print('  %-4s %-9s %s' % (g['id'], 'REQUIRED' if g['required'] else 'optional', g['question']))
        for o in g['options']:
            print('         %-28s %s' % (json.dumps(o['value']), o['label']))


if __name__ == '__main__':
    sys.exit(main())
