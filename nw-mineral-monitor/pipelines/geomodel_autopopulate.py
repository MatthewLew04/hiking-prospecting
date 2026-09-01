#!/usr/bin/env python3
"""geomodel_autopopulate — build a 3-D model for every mine the corpus describes.

The batch driver over the seams that already exist: ``geomodel_corpus``
supplies (mine, text) work units from the WS12 document store;
``services/minevis/tools.run_build`` — the exact code path the agent service
runs — parses, builds, renders and publishes each one; the result is a
per-mine index the map reads (``site/data/models/index.json``) plus a full
audit ledger (``var/geomodel/autopopulate-ledger.json``).

The question loop and the honesty rules are inherited, not reimplemented:

* One model per (mine, document).  Sections of the same document are one
  description; two documents describing the same mine are two models, and
  the index marks the strongest one ``primary``.
* **Answer policy = omit.**  Historic prose reliably leaves required fields
  open; unattended, every open question is answered ``value: null`` — "omit
  this element rather than guess it" — which is a legal, audited answer the
  service already supports.  Every omission lands in the model's
  ``manifest.json`` under ``answers`` with this module's name on it.
* Nothing is invented: a mine with documents but no parseable description
  gets an index entry with documents and no model, and a mine whose collar
  has no terrain tile is recorded, not placed at sea level.

Run it::

    python3 pipelines/geomodel_autopopulate.py            # everything
    python3 pipelines/geomodel_autopopulate.py --only grades:44 --no-context
    python3 pipelines/geomodel_autopopulate.py --dry-run  # parse + policy, no builds

Models land under ``<site>/models/<slug>-<hash8>/`` (the same layout the
minevis service publishes), so ``model3d.html?project=/models/<id>/…`` works
from the same site the map is served from.
"""

import argparse
import datetime
import json
import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'pipelines'))
sys.path.insert(0, os.path.join(ROOT, 'services'))

import geomodel_corpus as corpus                      # noqa: E402
from geomodel import narrative, publish, resolve      # noqa: E402
from minevis import jobs as minevis_jobs              # noqa: E402
from minevis import tools as minevis_tools            # noqa: E402

SITE_DIR = os.path.join(ROOT, 'site')
STATE_DIR = os.path.join(ROOT, 'var', 'geomodel', 'autopopulate-state')
LEDGER = os.path.join(ROOT, 'var', 'geomodel', 'autopopulate-ledger.json')
INDEX = os.path.join(SITE_DIR, 'data', 'models', 'index.json')

MAX_ROUNDS = 4
OMIT_BECAUSE = ('autopopulate: the source text does not state this; '
                'the element is omitted rather than guessed')

#: grade columns -> the mineral names a card can print
COMMODITY_NAMES = {'au': 'Gold', 'ag': 'Silver', 'pb': 'Lead', 'zn': 'Zinc',
                   'cu': 'Copper', 'sb': 'Antimony', 'wo3': 'Tungsten',
                   'hgf': 'Mercury'}


def _now():
    return datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def make_context(state_dir, target, base_url, zoom, offline, log):
    """A minevis tools.Context without the job worker threads.

    ``run_build`` needs the spec store (for the question loop) and the
    publish target; the job queue is the HTTP service's concern and this
    driver is synchronous, so a bare namespace stands in for it.  The target
    is always passed explicitly, so ``jobs.root`` is never consulted.
    """
    specs = minevis_jobs.SpecStore(state_dir)
    stub = types.SimpleNamespace(root=state_dir)
    return minevis_tools.Context(stub, specs, target=target, base_url=base_url,
                                 zoom=zoom, offline=offline, log=log)


def build_one(key, site_kind, site_ref, text, ctx, context=True, log=print):
    """One (mine, description) through the service's own build path.

    Returns ``{'state': 'done'|'skipped'|'error', ...}`` with the service's
    result, the answers the omit policy gave, and why a skip was a skip.
    """
    spec = narrative.parse(text, mine_id=key if site_kind == 'grades' else None)
    if not spec['elements']:
        return {'state': 'skipped', 'reason': 'no-elements',
                'coverage': spec['coverage'],
                'mentions': len(spec['mentions'])}

    args = {'text': text, 'context': bool(context)}
    if site_kind == 'grades':
        args['mine_id'] = key
    else:
        args['lon'], args['lat'] = site_ref['lon'], site_ref['lat']
        if site_ref.get('name'):
            args['name'] = site_ref['name']
    element_ids = {e['id'] for e in spec['elements']}
    omitted, answers_given = set(), []
    for _ in range(MAX_ROUNDS):
        state, result = minevis_tools.run_build(dict(args), ctx)
        if state != 'questions':
            break
        questions = result.get('questions') or []
        if not questions:
            return {'state': 'error', 'error': 'questions-without-questions',
                    'detail': result}
        new = [{'id': q['id'], 'value': None, 'because': OMIT_BECAUSE}
               for q in questions]
        answers_given.extend(new)
        omitted.update(q['element'] for q in questions if q.get('element'))
        if not element_ids - omitted:
            # every parsed element is now slated for omission; building would
            # only publish an empty model, so record the outcome instead
            return {'state': 'skipped', 'reason': 'all-elements-omitted',
                    'answers': answers_given, 'spec_id': result['spec_id']}
        args = {'spec_id': result['spec_id'], 'context': bool(context),
                'answers': new}
    else:
        return {'state': 'error', 'error': 'question-loop-did-not-converge',
                'answers': answers_given}

    if state == 'error':
        result = dict(result)
        result.setdefault('state', 'error')
        return {'state': 'error', 'error': result.get('error'),
                'detail': result.get('detail'), 'answers': answers_given}

    # emptiness is judged on built elements (workings AND stopes), not on the
    # workings line count alone — a stope is a mesh, not a line
    built_elements = sum((result.get('confidence') or {}).values())
    if not built_elements:
        # backstop: a build that still came out empty (e.g. only bare level
        # references survived) is an outcome to record, not a model to keep
        if isinstance(ctx.target, publish.LocalTarget) and result.get('key_prefix'):
            import shutil
            shutil.rmtree(os.path.join(ctx.target.root,
                                       *result['key_prefix'].split('/')),
                          ignore_errors=True)
        return {'state': 'skipped', 'reason': 'all-elements-omitted',
                'answers': answers_given, 'spec_id': result.get('spec_id')}
    out = {'state': 'done', 'answers': answers_given,
           'omitted_elements': sorted(omitted & element_ids)}
    held = ctx.specs.get(result.get('spec_id')) if result.get('spec_id') else None
    if held:
        held_spec = held.get('spec') or {}
        out['assay_commodities'] = sorted({a.get('commodity')
                                           for a in held_spec.get('assays') or ()
                                           if a.get('commodity')})
        out['level_depths_m'] = {k: v for k, v in
                                 (held_spec.get('levels') or {}).items()
                                 if isinstance(v, (int, float))}
    out.update(result)
    return out


def _merge_lexicon(into, add):
    for kind, entry in add.get('kinds', {}).items():
        slot = into['kinds'].setdefault(kind, {'count': 0, 'surfaces': {}})
        slot['count'] += entry['count']
        for surface, n in entry['surfaces'].items():
            slot['surfaces'][surface] = slot['surfaces'].get(surface, 0) + n
    for verb, n in add.get('verbs', {}).items():
        into['verbs'][verb] = into['verbs'].get(verb, 0) + n
    for lv in add.get('levels', ()):
        if lv['label'] not in into['level_labels']:
            into['level_labels'].append(lv['label'])
    into['sentences'] += add.get('sentences', 0)
    into['mining_sentences'] += add.get('mining_sentences', 0)


def _minerals(unit, grades, models):
    got = set()
    if unit['site_kind'] == 'grades':
        for row in unit.get('grade_rows') or ():
            for col, name in COMMODITY_NAMES.items():
                vals = grades.get(col)
                if vals and vals[row] is not None:
                    got.add(name)
    for model in models:
        for assay in model.get('assay_commodities') or ():
            name = COMMODITY_NAMES.get(assay)
            if name:
                got.add(name)
    return sorted(got)


def _extent(models):
    """What the strongest model says about where the mine goes.

    Depths come from the parser's own level table (label -> metres below the
    collar, unit conversion already done there), never re-derived from the
    label text — "300-metre level" and "300 level" are different depths.
    """
    primary = next((m for m in models if m.get('primary')), None)
    if primary is None:
        return None
    summary = primary.get('summary') or {}
    by_type = summary.get('by_type') or {}
    labels = sorted(primary.get('levels') or (),
                    key=lambda s: (not s.isdigit(), int(s) if s.isdigit() else 0, s))
    depths = [v for v in (primary.get('level_depths_m') or {}).values()]
    return {
        'total_m': round(summary.get('total_m') or 0.0, 1),
        'by_type': {k: round(v, 1) for k, v in sorted(by_type.items())},
        'levels': labels,
        'deepest_level_m': round(max(depths), 1) if depths else None,
    }


def run(units=None, site_dir=SITE_DIR, state_dir=STATE_DIR, base_url=None,
        zoom=13, offline=False, context=True, only=None, limit=None,
        dry_run=False, log=print, index_path=INDEX, ledger_path=LEDGER,
        grades=None):
    started = _now()
    got = corpus.assignments(log=log) if units is None else units
    units_list, parked = got['units'], got['parked']
    if only:
        wanted = set(only)

        def unit_matches(u):
            if u['key'] in wanted:
                return True
            # an alias row names the same mine: --only grades:868 must build
            # the canonical unit that row belongs to
            return any('grades:%d' % row in wanted
                       for row in u.get('grade_rows') or ())
        units_list = [u for u in units_list if unit_matches(u)]
    if limit:
        units_list = units_list[:limit]

    grades = grades if grades is not None else corpus._load_grades()
    os.makedirs(state_dir, exist_ok=True)
    target = publish.LocalTarget(site_dir)
    ctx = make_context(state_dir, target, base_url, zoom, offline, log)

    previous_by_mine = {}
    if os.path.exists(index_path):
        try:
            with open(index_path) as fh:
                previous_by_mine = json.load(fh).get('by_mine', {})
        except (OSError, ValueError):
            previous_by_mine = {}

    ledger_units, index_by_mine = [], {}
    had_errors = False
    for unit in units_list:
        key = unit['key']
        log('== %s (%s) — %d document(s), %d section(s)' % (
            key, unit['label'], len(unit['documents']), len(unit['texts'])))

        by_doc = {}
        for text in unit['texts']:
            by_doc.setdefault(text['doc_id'], []).append(text)

        models, builds = [], []
        lexicon = {'kinds': {}, 'verbs': {}, 'level_labels': [],
                   'sentences': 0, 'mining_sentences': 0}
        for doc_id, texts in sorted(by_doc.items()):
            combined = '\n\n'.join(t['text'] for t in texts)
            pages = sorted({p for t in texts for p in t['pages']})
            _merge_lexicon(lexicon, narrative.lexicon(combined))
            build = {'doc_id': doc_id, 'title': texts[0]['title'],
                     'source_url': texts[0]['source_url'],
                     'publication_year': texts[0]['publication_year'],
                     'pages': pages, 'chars': len(combined)}
            if dry_run:
                spec = narrative.parse(
                    combined, mine_id=key if unit['site_kind'] == 'grades' else None)
                build.update({'state': 'dry-run',
                              'elements': len(spec['elements']),
                              'required_gaps': len(narrative.unresolved(spec)),
                              'mentions': len(spec['mentions'])})
                builds.append(build)
                continue
            got_one = build_one(key, unit['site_kind'], unit['site_ref'],
                                combined, ctx, context=context, log=log)
            build.update({k: v for k, v in got_one.items()
                          if k not in ('exports', 'views', 'note')})
            builds.append(build)
            if got_one['state'] != 'done':
                log('   %s: %s (%s)' % (doc_id[:12], got_one['state'],
                                        got_one.get('reason') or got_one.get('error')))
                continue
            spec_assays = got_one.get('assays') or 0
            confidence = got_one.get('confidence') or {}
            omitted_elements = got_one.get('omitted_elements') or []
            models.append({
                'model_id': got_one['model_id'],
                'project_url': got_one.get('project_url'),
                'model_url': got_one.get('model_url'),
                'doc_id': doc_id, 'doc_title': texts[0]['title'],
                'source_url': texts[0]['source_url'],
                'publication_year': texts[0]['publication_year'],
                'pages': pages,
                'confidence': confidence,
                'elements': sum(confidence.values()) if confidence else None,
                'omitted': len(omitted_elements),
                'summary': got_one.get('summary'),
                'levels': got_one.get('levels'),
                'level_depths_m': got_one.get('level_depths_m') or {},
                'assay_commodities': got_one.get('assay_commodities') or [],
                'assays': spec_assays,
                'vein': bool(got_one.get('vein')),
                'republished': got_one.get('republished'),
            })
            log('   %s -> %s (%d described, %d assumed, %d element(s) omitted)' % (
                doc_id[:12], got_one['model_id'],
                confidence.get('described', 0), confidence.get('assumed', 0),
                len(omitted_elements)))

        def strength(m):
            c = m.get('confidence') or {}
            return (c.get('surveyed', 0) + c.get('described', 0),
                    (m.get('summary') or {}).get('total_m') or 0.0,
                    m.get('publication_year') or 0)   # newest description wins ties
        models.sort(key=strength, reverse=True)
        for i, model in enumerate(models):
            model['primary'] = (i == 0)

        entry = {
            'key': key, 'label': unit['label'],
            'site_kind': unit['site_kind'],
            'methods': unit['methods'],
            'store_mine_ids': unit['store_mine_ids'],
            'grade_rows': unit.get('grade_rows') or [],
            'documents': unit['documents'],
            'models': [{k: v for k, v in m.items()
                        if k not in ('summary', 'levels')} for m in models],
            'primary': models[0]['model_id'] if models else None,
            'lexicon': lexicon if unit['texts'] else None,
            'minerals': _minerals(unit, grades, models),
            'extent': _extent(models),
        }
        unit_errors = any(b.get('state') == 'error' for b in builds)
        had_errors = had_errors or unit_errors
        if unit_errors and not models:
            prev = previous_by_mine.get(key)
            if prev and not prev.get('alias') and prev.get('models'):
                # a transient failure must not erase a mine's published model:
                # the previous entry stands until a build actually succeeds
                log('   %s: builds errored; keeping the previous index entry' % key)
                entry = dict(prev)
                entry['carried_forward'] = started
        index_by_mine[key] = entry
        for row in entry['grade_rows']:
            alias = 'grades:%d' % row
            if alias != key:
                index_by_mine[alias] = {'alias': key}
        ledger_units.append({'unit': {k: v for k, v in unit.items() if k != 'texts'},
                             'texts': [{k: v for k, v in t.items() if k != 'text'}
                                       for t in unit['texts']],
                             'builds': builds})

    stats = dict(got['stats'])
    stats.update({
        'built_mines': sum(1 for e in index_by_mine.values()
                           if e.get('models')),
        'built_models': sum(len(e.get('models') or ()) for e in index_by_mine.values()),
        'dry_run': dry_run,
    })

    partial = bool(only or limit)
    if partial and previous_by_mine and not dry_run:
        # a scoped run updates its mines and leaves the rest of the index
        # alone — and the merged index's totals must describe the merge, not
        # the scoped slice that produced it
        merged = dict(previous_by_mine)
        merged.update(index_by_mine)
        index_by_mine = merged
        stats['built_mines'] = sum(1 for e in index_by_mine.values()
                                   if e.get('models'))
        stats['built_models'] = sum(len(e.get('models') or ())
                                    for e in index_by_mine.values())
        stats['partial_update'] = True

    if not dry_run:
        index = {'schema_version': 1, 'generated': started,
                 'grades_generated': grades.get('generated'),
                 'note': ('generated by pipelines/geomodel_autopopulate.py from the '
                          'WS12 document store; described geometry is drawn dashed and '
                          'is never a survey — see the model manifest for every quote '
                          'and every omission'),
                 'stats': stats, 'by_mine': index_by_mine}
        os.makedirs(os.path.dirname(index_path), exist_ok=True)
        tmp = index_path + '.tmp'
        with open(tmp, 'w') as fh:
            json.dump(index, fh, indent=1, sort_keys=True)
        os.replace(tmp, index_path)
        log('index: %s (%d mines, %d models)' % (
            os.path.relpath(index_path, ROOT), stats['mines'], stats['built_models']))
        if partial or had_errors or not stats['built_models']:
            # pruning is only safe when this run rebuilt the whole reference
            # set and every build resolved — a failed build leaves its model
            # unreferenced, and "absent because the build failed" must never
            # read as "superseded"
            if had_errors:
                log('prune skipped: this run had build errors')
        else:
            # a clean full run owns <site>/models entirely: content-addressing
            # means an edited description lands on a new hash, so anything the
            # fresh index no longer references is a stale earlier artifact
            keep = {m['model_id'] for e in index_by_mine.values()
                    for m in (e.get('models') or ())}
            models_root = os.path.join(site_dir, 'models')
            if os.path.isdir(models_root):
                import shutil
                for name in sorted(os.listdir(models_root)):
                    path = os.path.join(models_root, name)
                    if os.path.isdir(path) and name not in keep:
                        shutil.rmtree(path)
                        log('pruned stale model %s' % name)

    ledger = {'schema_version': 1, 'started': started, 'finished': _now(),
              'stats': stats, 'units': ledger_units, 'parked': parked}
    os.makedirs(os.path.dirname(ledger_path), exist_ok=True)
    tmp = ledger_path + '.tmp'
    with open(tmp, 'w') as fh:
        json.dump(ledger, fh, indent=1, sort_keys=True)
    os.replace(tmp, ledger_path)
    log('ledger: %s' % os.path.relpath(ledger_path, ROOT))
    return ledger


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--site-dir', default=SITE_DIR,
                    help='models are published under <site-dir>/models/')
    ap.add_argument('--state-dir', default=STATE_DIR)
    ap.add_argument('--base-url', default=None,
                    help='public base for returned URLs (default: relative paths)')
    ap.add_argument('--zoom', type=int, default=13)
    ap.add_argument('--offline', action='store_true',
                    help='never fetch terrain; unbuildable mines are recorded')
    ap.add_argument('--no-context', dest='context', action='store_false',
                    help='workings only — skip terrain/geology context scenes')
    ap.add_argument('--only', action='append',
                    help='build only this mine key (repeatable), e.g. grades:44')
    ap.add_argument('--limit', type=int, default=None)
    ap.add_argument('--dry-run', action='store_true',
                    help='parse and report; build nothing, write no index')
    args = ap.parse_args(argv)
    ledger = run(site_dir=args.site_dir, state_dir=args.state_dir,
                 base_url=args.base_url, zoom=args.zoom, offline=args.offline,
                 context=args.context, only=args.only, limit=args.limit,
                 dry_run=args.dry_run)
    print(json.dumps(ledger['stats'], indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
