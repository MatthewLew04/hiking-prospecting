"""minevis.tools — the OpenAI function schemas the agent is given, and the
dispatch behind them.

``TOOLS`` is the array to paste straight into an OpenAI-style ``tools``
parameter; ``GET /tools`` returns exactly it.

Two of these are synchronous and touch nothing outside this box:
``mine_lookup`` and ``parse_mine_description``.  ``build_mine_visual`` returns
a ``job_id`` because the first build in a district has to fetch terrain tiles.

The question round trip is the shape the whole service is built around::

    build_mine_visual(text=..., mine_id=...)  -> {job_id}
    get_job(job_id)                           -> {questions: [...], spec_id}
    build_mine_visual(spec_id=..., answers=[...])
                                              -> {job_id}
    get_job(job_id)                           -> {model_url, views, exports, ...}

Both builds run the same deterministic builder.  An answer is recorded in
``manifest.json`` tagged ``assumed`` with whatever justification the agent
gave, so an auditor can separate the numbers that came out of the document
from the numbers that came out of the model.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..', '..'))
if os.path.join(ROOT, 'pipelines') not in sys.path:
    sys.path.insert(0, os.path.join(ROOT, 'pipelines'))

from geomodel import agentbuild, assay, mapplate, narrative, publish, render2d, resolve  # noqa: E402

DOCS_INDEX = os.path.join(ROOT, 'site', 'data', 'docs', 'index.json')

VIEWS = ('plan', 'section', 'iso')


def _fn(name, description, properties, required=()):
    return {'type': 'function', 'function': {
        'name': name, 'description': description,
        'parameters': {'type': 'object', 'properties': properties,
                       'required': list(required), 'additionalProperties': False}}}



#: one scanned plate plus what has been traced on it
PLATE_PROPERTIES = {
    'plate_id': {'type': 'string', 'description': 'short id for this plate, e.g. "p3"'},
    'image': {'type': 'string', 'description': 'URL or path of the scan'},
    'width': {'type': 'integer', 'description': "the scan's width in pixels"},
    'height': {'type': 'integer', 'description': "the scan's height in pixels"},
    'plane': {'type': 'string', 'enum': ['plan', 'section'],
              'description': 'a level plan / surface map, or a vertical section'},
    'control': {'type': 'array',
                'description': 'plan georeference: >= 2 tie points [px, py, lon, lat]',
                'items': {'type': 'array', 'items': {'type': 'number'},
                          'minItems': 4, 'maxItems': 4}},
    'anchor': {'type': 'object',
               'description': 'plan georeference from one known point and a scale bar',
               'properties': {
                   'px': {'type': 'array', 'items': {'type': 'number'},
                          'description': 'pixel [x, y] of the known point'},
                   'lonlat': {'type': 'array', 'items': {'type': 'number'},
                              'description': '[lon, lat] of the known point'},
                   'scale_m_per_px': {'type': 'number', 'description': 'metres per pixel'},
                   'rotation_deg': {'type': 'number',
                                    'description': "image up-direction clockwise from north"}},
               'additionalProperties': False},
    'level': {'type': 'string', 'description': 'the level this plan is drawn at, e.g. "300"'},
    'elevation_m': {'type': 'number',
                    'description': "the plan's elevation, if it is written on the plate"},
    'p1': {'type': 'array', 'items': {'type': 'number'},
           'description': "section: [lon, lat] of the image's top-left corner"},
    'p2': {'type': 'array', 'items': {'type': 'number'},
           'description': "section: [lon, lat] of the image's top-right corner"},
    'z_top': {'type': 'number', 'description': "section: elevation of the image's top edge"},
    'z_bottom': {'type': 'number', 'description': "section: elevation of the bottom edge"},
    'source': {'type': 'object', 'description': 'citation: doc, page, figure, url',
               'properties': {'doc': {'type': 'string', 'description': 'publication title'},
                              'page': {'type': 'string', 'description': 'page number'},
                              'figure': {'type': 'string', 'description': 'plate or figure label'},
                              'url': {'type': 'string', 'description': 'link to the scan'}},
               'additionalProperties': False},
    'traces': {'type': 'array', 'description': 'polylines traced on the plate, in pixels',
               'items': {'type': 'object', 'properties': {
                   'id': {'type': 'string', 'description': 'short id, e.g. "t1"'},
                   'kind': {'type': 'string', 'enum': list(mapplate.TRACEABLE),
                            'description': 'what this working is'},
                   'name': {'type': 'string', 'description': 'name as written on the plate'},
                   'level': {'type': 'string',
                             'description': 'level, when it differs from the plate\'s'},
                   'points': {'type': 'array', 'description': 'pixel [x, y] along the working',
                              'items': {'type': 'array', 'items': {'type': 'number'},
                                        'minItems': 2, 'maxItems': 2}}},
                   'required': ['kind', 'points'], 'additionalProperties': False}},
}

TOOLS = [
    _fn('mine_lookup',
        'Find a mine in the NW Mineral Monitor grades bundle (3,369 cited historic mines) '
        'and get its coordinate, district and source citation. Returns CANDIDATES, never a '
        'single answer: historic mining names collide constantly, so you must choose which '
        'one is meant before building anything.',
        {'name': {'type': 'string', 'description': 'the mine name as written in the source'},
         'state': {'type': 'string', 'description': 'two-letter state code; a mismatch rejects a candidate'},
         'district': {'type': 'string', 'description': 'mining district, if known'},
         'county': {'type': 'string', 'description': 'county, if known'}},
        ('name',)),

    _fn('parse_mine_description',
        'Parse a written description of a mine (USGS/USBM-style prose) into typed workings '
        'elements plus the questions the prose does not answer. Deterministic and offline. '
        'Nothing is invented: a missing bearing comes back as a question, never a default. '
        'Workings the text names without describing ("developed by two adits") come back '
        'as "mentions" rather than elements, because they cannot be built. Grades quoted in '
        'the same text come back as "assays", each keeping its basis - a selected sample is '
        'not an average - and a stated vein strike and dip come back as "vein". '
        'Use this to see what a description will produce before committing to a build.',
        {'text': {'type': 'string', 'description': 'the description, verbatim'},
         'mine_id': {'type': 'string', 'description': 'a mine_id from mine_lookup, e.g. "grades:17"'}},
        ('text',)),

    _fn('build_mine_visual',
        'Build a 3-D model of a mine from a description and publish it. Returns a job_id; '
        'poll get_job. The job finishes as "done" with a model_url, or as "questions" with '
        'the things the description left open — answer those and call this again with '
        'spec_id and answers. Give either text or spec_id, and either mine_id or lon/lat.',
        {'text': {'type': 'string', 'description': 'the description, verbatim'},
         'spec_id': {'type': 'string',
                     'description': 'a spec_id from an earlier parse or a "questions" job; '
                                    'use this when answering questions'},
         'mine_id': {'type': 'string', 'description': 'a mine_id from mine_lookup, e.g. "grades:17"'},
         'lon': {'type': 'number', 'description': 'longitude, if the mine is not in the bundle'},
         'lat': {'type': 'number', 'description': 'latitude, if the mine is not in the bundle'},
         'answers': {'type': 'array', 'description': 'answers to questions from a previous job',
                     'items': {'type': 'object', 'properties': {
                         'id': {'type': 'string', 'description': 'the question id, e.g. "g1"'},
                         'value': {'description': 'the value; null omits the element rather than guessing'},
                         'because': {'type': 'string',
                                     'description': 'why — recorded verbatim in the audit manifest'}},
                         'required': ['id', 'value'], 'additionalProperties': False}},
         'views': {'type': 'array', 'description': 'which 2-D views to render (default all three)',
                   'items': {'type': 'string', 'enum': list(VIEWS)}},
         'context': {'type': 'boolean',
                     'description': 'also build terrain, draped geology and grade points around '
                                    'the mine; slower, fetches tiles'},
         'plates': {'type': 'array',
                    'description': 'scanned plans or sections with workings traced on them. '
                                   'These build at surveyed confidence and draw solid, unlike '
                                   'anything read from prose. Re-sending a plate replaces it.',
                    'items': {'type': 'object', 'properties': PLATE_PROPERTIES,
                              'additionalProperties': False}}},
        ()),

    _fn('check_map_plate',
        'Check the georeference of a scanned level plan or section before building with it, '
        'and see what it still needs. Returns the implied metres-per-pixel and, with three or '
        'more control points, how far they disagree - a large residual means the plate was '
        'tied wrongly. Workings traced off a georeferenced plate are the ONLY way to get '
        'surveyed confidence; everything read from prose is described at best.',
        {'plate': {'type': 'object', 'description': 'the plate and its traces',
                   'properties': PLATE_PROPERTIES, 'additionalProperties': False}},
        ('plate',)),

    _fn('get_job',
        'Poll a build. state is one of queued, running, done, questions, error. '
        '"questions" is a normal outcome, not a failure.',
        {'job_id': {'type': 'string', 'description': 'the job_id from build_mine_visual'}},
        ('job_id',)),

    _fn('list_mine_documents',
        'List the scanned source documents held for a mine, so you can read the prose '
        'yourself instead of being handed it. Returns titles, page counts and source URLs.',
        {'mine_id': {'type': 'string', 'description': 'a mine_id from mine_lookup'},
         'name': {'type': 'string', 'description': 'mine name, when you have no mine_id'}},
        ()),
]

TOOL_NAMES = [t['function']['name'] for t in TOOLS]

SYNC = ('mine_lookup', 'parse_mine_description', 'check_map_plate', 'get_job',
        'list_mine_documents')
ASYNC = ('build_mine_visual',)


class ToolError(ValueError):
    """A bad call.  Becomes a 400 with a message the agent can act on."""


# --------------------------------------------------------------- the context
class Context(object):
    """Everything the tools need that is not an argument: the stores, where
    models go, and what the public URL of a published model looks like."""

    def __init__(self, jobs, specs, target=None, base_url=None, zoom=13, offline=False,
                 log=print):
        self.jobs = jobs
        self.specs = specs
        self.log = log
        self.zoom = zoom
        self.offline = offline
        self.base_url = publish.base_url_from_env(base_url)
        bucket = os.environ.get('NWMM_MODELS_BUCKET')
        if target is not None:
            self.target, self.target_kind = target, 'given'
        elif bucket:
            self.target, self.target_kind = publish.target_from_env(bucket), 's3'
        else:
            # No bucket configured: publish to disk and say so.  This is what
            # makes the service provable end to end with no AWS at all.
            # LocalTarget mirrors the bucket layout, so its root is the state
            # directory and the keys it writes start with "models/"
            self.target, self.target_kind = publish.LocalTarget(jobs.root), 'local'
            self.local_models = os.path.join(jobs.root, publish.PREFIX)


def dispatch(name, arguments, ctx):
    """``(kind, payload)`` where kind is 'result' or 'job'."""
    if name not in TOOL_NAMES:
        raise ToolError('unknown tool %r; this service offers: %s' % (name, ', '.join(TOOL_NAMES)))
    if not isinstance(arguments, dict):
        raise ToolError('arguments must be an object')
    if name in ASYNC:
        _precheck_build(arguments)
        return 'job', {'job_id': ctx.jobs.submit(name, arguments)}
    return 'result', SYNC_IMPL[name](arguments, ctx)


# --------------------------------------------------------- synchronous tools
def _mine_lookup(args, ctx):
    name = args.get('name')
    if not name:
        raise ToolError('mine_lookup needs a name')
    got = resolve.lookup(name, args.get('state'), args.get('district'), args.get('county'))
    got['question'] = resolve.which_mine_gap(got) if got['ambiguous'] else None
    return got


def _parse_mine_description(args, ctx):
    text = args.get('text')
    if not isinstance(text, str) or not text.strip():
        raise ToolError('parse_mine_description needs some text')
    spec = assay.attach(narrative.parse(text, mine_id=args.get('mine_id')), text)
    ctx.specs.put(spec)
    return {'spec_id': spec['spec_id'], 'elements': spec['elements'],
            'mentions': spec['mentions'], 'gaps': spec['gaps'],
            'assays': spec['assays'], 'vein': spec['vein'],
            'coverage': spec['coverage'], 'levels': spec['levels'],
            'parser_version': spec['parser_version']}


def _check_map_plate(args, ctx):
    raw = args.get('plate')
    if not isinstance(raw, dict):
        raise ToolError('check_map_plate needs a plate object')
    try:
        plate = mapplate.validate_plate(raw)
        traces = mapplate.validate_traces(plate, raw.get('traces'))
    except mapplate.PlateError as exc:
        raise ToolError(str(exc))
    gaps = mapplate.plate_gaps(plate, traces)
    out = {'plate_id': plate['plate_id'], 'plane': plate['plane'],
           'citation': mapplate.citation(plate), 'traces': len(traces),
           'questions': gaps, 'usable': not any(g['required'] for g in gaps)}
    if out['usable']:
        out['scale'] = mapplate.scale_check(plate)
        out['note'] = ('workings traced off this plate will build at "surveyed" confidence '
                       'and draw solid')
    return out


def _get_job(args, ctx):
    job_id = args.get('job_id')
    try:
        rec = ctx.jobs.read(job_id)
    except KeyError:
        raise ToolError('job_id must look like "j-0123456789abcdef", got %r' % (job_id,))
    if rec is None:
        raise ToolError('no such job: %r' % (job_id,))
    out = {'job_id': rec['job_id'], 'state': rec['state'],
           'created_utc': rec.get('created_utc'), 'updated_utc': rec.get('updated_utc')}
    if rec.get('result'):
        out.update(rec['result'])
    return out


def _list_mine_documents(args, ctx):
    """The WS12 document store, filtered to one mine.  Reports what it actually
    holds rather than implying the whole corpus is here."""
    import json

    try:
        with open(DOCS_INDEX, encoding='utf-8') as fh:
            index = json.load(fh)
    except (OSError, IOError, ValueError):
        return {'documents': [], 'available': False,
                'note': 'no document index is installed on this box (%s)' % DOCS_INDEX}
    docs = index.get('documents') or []
    want_id, want_name = args.get('mine_id'), (args.get('name') or '').strip()
    if not want_id and not want_name:
        raise ToolError('list_mine_documents needs a mine_id or a name')

    keys = set()
    if want_id:
        keys.add(want_id)
        if want_id.startswith('grades:'):
            try:
                keys.add(resolve.load_index().get(want_id)['name'])
            except (KeyError, ValueError, OSError):
                pass
    norm = set(resolve.normalise(k) for k in keys if k)
    if want_name:
        norm.add(resolve.normalise(want_name))

    hits = []
    for doc in docs:
        names = set(resolve.normalise(n) for n in (doc.get('mine_names') or []))
        ids = set(doc.get('mine_ids') or [])
        if not (names & norm) and not (ids & keys):
            continue
        hits.append({k: doc.get(k) for k in
                     ('document_id', 'title', 'doc_type', 'doc_date', 'state', 'county',
                      'page_count', 'indexed_pages', 'source_url', 'mine_names', 'trs')})
    return {'documents': hits, 'available': True,
            'index_documents': len(docs), 'index_generated': index.get('generated'),
            'note': 'this is the published document index; the full store may hold more'}


SYNC_IMPL = {
    'mine_lookup': _mine_lookup,
    'parse_mine_description': _parse_mine_description,
    'check_map_plate': _check_map_plate,
    'get_job': _get_job,
    'list_mine_documents': _list_mine_documents,
}


# ---------------------------------------------------------- the build itself
def _precheck_build(args):
    """Reject a call that cannot possibly work before it costs a job slot."""
    if not args.get('text') and not args.get('spec_id'):
        raise ToolError('build_mine_visual needs either text or spec_id')
    if args.get('spec_id') and args.get('text'):
        raise ToolError('give text or spec_id, not both')
    if args.get('answers') is not None and not isinstance(args['answers'], list):
        raise ToolError('answers must be a list of {id, value, because}')
    if args.get('plates') is not None and not isinstance(args['plates'], list):
        raise ToolError('plates must be a list of plate objects')
    views = args.get('views')
    if views is not None:
        bad = [v for v in views if v not in VIEWS]
        if bad:
            raise ToolError('unknown view(s) %s; choose from %s' % (bad, list(VIEWS)))
    if (args.get('lon') is None) != (args.get('lat') is None):
        raise ToolError('lon and lat must be given together')


def run_build(args, ctx):
    """The body of ``build_mine_visual``.  Returns ``(state, result)``."""
    spec, site = _load_spec(args, ctx)
    answers = args.get('answers') or []
    if answers:
        try:
            spec = narrative.apply_answers(spec, answers)
        except ValueError as exc:
            return 'error', {'error': 'bad_answer', 'detail': str(exc),
                             'spec_id': spec['spec_id']}
        ctx.specs.put(spec, site)

    if site is None:
        return 'questions', {'spec_id': spec['spec_id'],
                             'questions': [_no_mine_question()],
                             'note': 'the mine has not been identified yet'}

    if args.get('plates') is not None:
        try:
            spec = mapplate.attach(spec, args['plates'])
        except mapplate.PlateError as exc:
            return 'error', {'error': 'bad_plate', 'detail': str(exc),
                             'spec_id': spec['spec_id']}
        ctx.specs.put(spec, site)

    pending = narrative.unresolved(spec)
    if pending:
        ctx.specs.put(spec, site)
        return 'questions', {'spec_id': spec['spec_id'], 'questions': pending,
                             'mine': _mine_brief(site),
                             'coverage': spec['coverage'],
                             'note': 'answer these with build_mine_visual(spec_id=..., answers=[...]); '
                                     'a null value omits that element rather than guessing it'}

    try:
        built = agentbuild.build(spec, site, context=bool(args.get('context')),
                                 zoom=ctx.zoom, offline=ctx.offline, log=ctx.log)
    except agentbuild.Unplaceable as exc:
        return 'error', {'error': 'unplaceable', 'detail': str(exc),
                         'spec_id': spec['spec_id'], 'mine': _mine_brief(site)}

    if built['gaps']:
        ctx.specs.put(spec, site)
        return 'questions', {'spec_id': spec['spec_id'], 'questions': built['gaps'],
                             'mine': _mine_brief(site), 'placed': len(built['placed']),
                             'note': 'these elements parsed cleanly but cannot be located; '
                                     'answering attaches them to something'}

    views = render2d.render(built, views=tuple(args.get('views') or VIEWS))
    result = publish.publish(built, spec, site, views=views, target=ctx.target,
                             base_url=ctx.base_url, log=ctx.log)
    result['spec_id'] = spec['spec_id']
    result['mine'] = _mine_brief(site)
    result['summary'] = built['summary']
    result['levels'] = built['levels']
    result['coverage'] = spec['coverage']
    result['assays'] = built['assays']
    result['vein'] = built['vein']
    result['storage'] = ctx.target_kind
    if ctx.target_kind == 'local':
        result['note'] = ('no models bucket is configured on this box, so the model was '
                          'written to local disk and the URLs are paths under this service')
    return 'done', result


def _load_spec(args, ctx):
    if args.get('spec_id'):
        held = ctx.specs.get(args['spec_id'])
        if held is None:
            raise ToolError('no spec %r is held; parse the description again'
                            % (args['spec_id'],))
        spec, site = held['spec'], held.get('site')
        if args.get('mine_id') or args.get('lon') is not None:
            site = _resolve_site(args, ctx)
        return spec, site
    spec = assay.attach(narrative.parse(args['text'], mine_id=args.get('mine_id')), args['text'])
    site = _resolve_site(args, ctx)
    ctx.specs.put(spec, site)
    return spec, site


def _resolve_site(args, ctx):
    if args.get('mine_id'):
        try:
            return resolve.site(args['mine_id'], zoom=ctx.zoom, offline=ctx.offline)
        except (KeyError, ValueError) as exc:
            raise ToolError('%s — use mine_lookup to get a valid mine_id' % exc)
    if args.get('lon') is not None and args.get('lat') is not None:
        lon, lat = float(args['lon']), float(args['lat'])
        z = resolve.elevation(lon, lat, zoom=ctx.zoom, offline=ctx.offline)
        return {'mine_id': None, 'name': args.get('name') or 'mine at %.5f, %.5f' % (lat, lon),
                'lon': lon, 'lat': lat, 'elevation_m': z,
                'elevation_source': ('AWS Terrain Tiles (terrarium), zoom %d' % ctx.zoom
                                     if z is not None else 'terrain tile unavailable'),
                'terrain_zoom': ctx.zoom, 'source': '', 'source_url': ''}
    return None


def _mine_brief(site):
    return {k: site.get(k) for k in ('mine_id', 'name', 'state', 'district', 'lon', 'lat',
                                     'elevation_m', 'elevation_source', 'source', 'source_url')}


def _no_mine_question():
    return {'id': 'which_mine', 'field': 'mine_id', 'required': True, 'kind': 'which_mine',
            'question': 'Which mine is this? Call mine_lookup, choose a candidate, and call '
                        'build_mine_visual again with that mine_id — or give lon and lat.',
            'options': [], 'quote': '', 'span': None}


RUNNERS = {'build_mine_visual': run_build}


def make_runner(ctx):
    """The callable the job store runs.  Kept here so the store knows nothing
    about mines."""
    def runner(name, arguments):
        fn = RUNNERS.get(name)
        if fn is None:
            return 'error', {'error': 'unknown_job', 'detail': name}
        return fn(arguments, ctx)
    return runner
