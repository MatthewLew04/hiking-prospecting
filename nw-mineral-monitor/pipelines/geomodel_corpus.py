#!/usr/bin/env python3
"""geomodel_corpus — the WS12 document store, read per mine.

The bridge between the stored source documents and the 3-D modeller: which
document belongs to which buildable mine, and which stretch of its text
describes that mine's underground workings.

Three layers, each deterministic and recorded:

1. **Text** — the store's ``searchable.pdf`` files carry a text layer
   (native or OCR, written by ``build_doc_store.py``); ``page_texts`` extracts
   it per page with PyMuPDF and caches the result under
   ``var/geomodel/pagetext/`` keyed by document sha256.
2. **Bridge** — a document subject or citation names a mine in the store's
   namespaces (``ws9-*``, ``stategeo-*``, ``district-*``); the modeller builds
   only from a grades-bundle row (``grades:N``) or a bare coordinate.  The
   bridge tiers, strictest first, each stamped on the link it produces:

   ============== =====================================================
   method          how the document mine becomes a buildable reference
   ============== =====================================================
   citation_quote  the citation's verbatim quote equals exactly one
                   grades-bundle row's quote
   evidence_name   the ``ws9-*`` id resolves through the reviewed grade
                   evidence to a name+state that matches exactly one
                   bundle row (``resolve.normalise`` on both sides)
   subject_lookup  the subject label+state gives ``resolve.lookup`` a
                   single unambiguous, located candidate
   stategeo_site   a ``stategeo-*`` id matches a state-survey site
                   record with coordinates (built by lon/lat)
   ============== =====================================================

   Anything else is **parked with a reason, never guessed** — the same rule
   the parser applies to a missing bearing.
3. **Sections** — a district report describes many mines; feeding a whole
   page to the parser would attribute one mine's adit to another.
   ``sections`` carves name-anchored windows: each window starts where the
   mine's name (or a located citation quote) appears and ends at the first
   mention of a *different* subject of the same document, so the text handed
   to ``narrative.parse`` is the stretch that is about this mine.  Windows
   with no mining language are dropped (tables of contents mention every
   mine; they describe none).

Unlike ``pipelines/geomodel/`` (stdlib-pure by policy), this module may use
PyMuPDF — the same dependency ``build_doc_store.py`` already requires.
"""

import glob
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'pipelines'))

from geomodel import narrative, resolve  # noqa: E402

STORE_MANIFEST = os.path.join(ROOT, 'var', 'ws12', 'document-store-manifest.json')
STORE_ROOT = os.path.join(ROOT, 'pipelines', 'cache', 'ws12', 'store')
PAGETEXT_CACHE = os.path.join(ROOT, 'var', 'geomodel', 'pagetext')
EVIDENCE_GLOB = os.path.join(ROOT, 'grades-research', '*', 'reviewed_grade_evidence.json')
SITES_DIR = os.path.join(ROOT, 'build-inputs', 'data', 'sites')

#: how far a name-anchored window may run when no other subject cuts it off
MAX_SECTION_CHARS = 6000

#: suffixes that decorate a mine label but are not part of the name core
_LABEL_SUFFIX = re.compile(
    r'\s+(mine|mines|group|claim|claims|lease|leases|tunnel|shaft|prospect|'
    r'prospects|property|workings|deposit|lode|vein)$', re.I)


def _fail(msg):
    raise SystemExit(msg)


# --------------------------------------------------------------------- text

def load_manifest(path=None):
    with open(path or STORE_MANIFEST) as fh:
        return json.load(fh)


def page_texts(doc, store_root=None, cache_dir=None):
    """Per-page text of a store document's searchable.pdf, cached by sha256.

    The cache is keyed by the document id (its sha256) so a re-stored
    document with different bytes never reuses a stale extraction.
    """
    doc_id = doc['doc_id']
    cache_dir = cache_dir or PAGETEXT_CACHE
    cache = os.path.join(cache_dir, doc_id + '.json')
    if os.path.exists(cache):
        with open(cache) as fh:
            got = json.load(fh)
        if got.get('doc_id') == doc_id:
            return got['pages']
    key = doc['searchable']['key'] if isinstance(doc.get('searchable'), dict) \
        else doc.get('searchable')
    if not key:
        _fail('document %s has no searchable key' % doc_id)
    path = os.path.join(store_root or STORE_ROOT, key)
    try:
        import fitz
    except ImportError as exc:  # pragma: no cover - environment, not logic
        _fail('PyMuPDF (fitz) is required to read the document store text '
              'layer (same requirement as build_doc_store.py): %s' % exc)
    with fitz.open(path) as pdf:
        pages = [page.get_text() or '' for page in pdf]
    os.makedirs(cache_dir, exist_ok=True)
    tmp = cache + '.tmp'
    with open(tmp, 'w') as fh:
        json.dump({'doc_id': doc_id, 'pages': pages}, fh)
    os.replace(tmp, cache)
    return pages


def core_label(label):
    """'Bullion mine (Gulch/Eureka)' -> 'Bullion' — the searchable name core."""
    x = re.sub(r'\s*\(.*\)\s*$', '', str(label or '')).strip()
    while True:
        y = _LABEL_SUFFIX.sub('', x).strip()
        if y == x:
            return x
        x = y


def slug(value):
    return re.sub(r'-+', '-', re.sub(r'[^a-z0-9]+', '-', str(value or '').lower())).strip('-')


# ------------------------------------------------------------------- bridge

def _load_grades(path=None):
    with open(path or resolve.GRADES) as fh:
        return json.load(fh)


def _load_evidence(pattern=None):
    """ws9-<id> -> {'name', 'state', 'district', 'county'} from the reviewed files."""
    out = {}
    for path in sorted(glob.glob(pattern or EVIDENCE_GLOB)):
        with open(path) as fh:
            data = json.load(fh)
        for rec in data.get('mines', ()):
            out['ws9-' + rec['mine_id']] = {
                'name': rec.get('name', ''), 'state': data.get('state'),
                'district': rec.get('district'), 'county': rec.get('county')}
    return out


def _load_stategeo_sites(sites_dir=None):
    """slug('stategeo:<id>') -> {'front_end_id', 'name', 'lon', 'lat', 'state'}."""
    out = {}
    for path in sorted(glob.glob(os.path.join(sites_dir or SITES_DIR, 'stategeo_*.json'))):
        with open(path) as fh:
            data = json.load(fh)
        ids, names = data.get('id', ()), data.get('nm', ())
        xs, ys = data.get('x', ()), data.get('y', ())
        state = data.get('state')
        for i, sid in enumerate(ids):
            if xs[i] is None or ys[i] is None:
                continue
            out[slug('stategeo:%s' % sid)] = {
                'front_end_id': 'stategeo:%s' % sid, 'name': names[i],
                'lon': xs[i], 'lat': ys[i], 'state': state}
    return out


def _mild(name):
    """Name key for deciding two bundle rows are one physical mine.

    Strips the parenthetical qualifier, punctuation and the mine-word
    suffixes ("Monocco claim" and "Monocco Mine" are one hole in the
    ground), but — unlike ``resolve.normalise`` — never the container words:
    "Tintic district" must not weld to a "Tintic mine".  ``_LABEL_SUFFIX``
    is exactly that list.
    """
    x = re.sub(r'\s*\(.*\)\s*$', '', str(name or ''))
    x = re.sub(r'\s+', ' ', re.sub(r'[^A-Za-z0-9]+', ' ', x)).strip()
    while True:
        y = _LABEL_SUFFIX.sub('', x).strip()
        if y == x:
            return x.lower()
        x = y


#: located rows farther apart than this are different holes in the ground,
#: whatever their names say (degrees; ~10 km at these latitudes)
_GROUP_COORD_TOL = 0.1


def _grade_groups(grades):
    """Rows of the same physical mine, collapsed.

    The grades bundle holds one row per cited figure, so one mine is often
    several rows ("Victoria mine (1,050-foot level, rich ore)" and "Victoria
    mine (shipping ore)").  Rows sharing a mild name key + state are one
    candidate bucket; within it, two *different named* districts are two
    mines, while a row with no district recorded (many bundle sources carry
    none) folds into the bucket's single named district if there is exactly
    one.  Located rows that then disagree beyond ``_GROUP_COORD_TOL`` are
    split back into their own groups — two same-named mines 400 km apart are
    two mines whatever the columns say.  The canonical row is the lowest
    located one; a group with no located row has no buildable coordinate.
    """
    dist = grades.get('dist') or [None] * grades['n']
    buckets = {}
    for i in range(grades['n']):
        buckets.setdefault((_mild(grades['name'][i]), grades['st'][i]), []).append(i)

    canon = {}

    def close(a, b):
        return (abs(grades['x'][a] - grades['x'][b]) <= _GROUP_COORD_TOL and
                abs(grades['y'][a] - grades['y'][b]) <= _GROUP_COORD_TOL)

    def settle(rows):
        located = [r for r in rows
                   if grades['x'][r] is not None and grades['y'][r] is not None]
        xs = [grades['x'][r] for r in located]
        ys = [grades['y'][r] for r in located]
        agree = (not located or
                 (max(xs) - min(xs) <= _GROUP_COORD_TOL and
                  max(ys) - min(ys) <= _GROUP_COORD_TOL))
        if agree:
            rep = min(located) if located else None
            for r in rows:
                canon[r] = rep
        else:
            for r in rows:
                canon[r] = r if r in located else None

    for rows in buckets.values():
        named = {}
        bare = []
        for r in rows:
            key = _mild(dist[r])
            if key:
                named.setdefault(key, []).append(r)
            else:
                bare.append(r)
        if len(named) <= 1:
            settle(rows)
            continue
        # several named districts: a bare-district row joins the one district
        # whose located rows it sits beside; otherwise it stays alone
        for key, sub in named.items():
            sub_located = [r for r in sub
                           if grades['x'][r] is not None and grades['y'][r] is not None]
            take = []
            for r in list(bare):
                if grades['x'][r] is None or grades['y'][r] is None:
                    continue
                near = [k for k, s in named.items()
                        if any(grades['x'][q] is not None and close(r, q) for q in s)]
                if near == [key]:
                    take.append(r)
                    bare.remove(r)
            settle(sub + take)
        for r in bare:
            canon[r] = r if (grades['x'][r] is not None and
                             grades['y'][r] is not None) else None
    groups = {}
    for r, rep in canon.items():
        groups.setdefault(rep, []).append(r)
    return groups, canon


def build_bridges(manifest, grades=None, evidence=None, stategeo=None):
    """Everything the store says about a mine -> a buildable reference, or parked.

    Returns ``{'links', 'citation_links', 'groups', 'parked'}``:

    * ``links`` — subject-tier results, ``{store_mine_id: link}`` where a link
      is ``{'kind': 'grades', 'mine_id': 'grades:<canonical row>', 'rows',
      'method', 'name'}`` or ``{'kind': 'latlon', 'front_end_id', 'lon',
      'lat', 'name', 'method'}``.
    * ``citation_links`` — one entry per reviewed citation whose verbatim
      quote equals exactly one bundle row's quote; a district report's
      citations each name their own mine, so these are per-citation, never
      per-document.
    * ``groups`` — ``{'grades:<canonical>': [every row of that mine]}``.

    A conflict between tiers parks the id instead of choosing.
    """
    grades = grades if grades is not None else _load_grades()
    evidence = evidence if evidence is not None else _load_evidence()
    stategeo = stategeo if stategeo is not None else _load_stategeo_sites()
    groups, canon = _grade_groups(grades)

    by_quote = {}
    for i in range(grades['n']):
        q = (grades['quote'][i] or '').strip()
        if q:
            by_quote.setdefault(q, []).append(i)
    by_name = {}
    for i in range(grades['n']):
        by_name.setdefault(
            (resolve.normalise(grades['name'][i]), grades['st'][i]), []).append(i)

    def grades_link(row, method):
        rep = canon[row]
        if rep is None:
            return None
        return {'kind': 'grades', 'mine_id': 'grades:%d' % rep, 'method': method,
                'name': grades['name'][rep],
                'rows': sorted(r for r in canon if canon[r] == rep), 'located': True}

    def names_agree(a, b):
        """The citation's mine name and the row's must be the same mine's name.

        Quote equality alone is not identity — a boilerplate sentence can
        appear under two mines, and welding them attributes one mine's model
        to another.  Normalised equality or a token-subset either way.
        """
        na, nb = resolve.normalise(a), resolve.normalise(b)
        if not na or not nb:
            return False
        ta, tb = set(na.split()), set(nb.split())
        return na == nb or ta <= tb or tb <= ta

    citation_links, parked = [], []
    for cit in manifest.get('citations', ()):
        rows = by_quote.get((cit.get('quote') or '').strip(), ())
        if len(rows) != 1:
            continue
        row = rows[0]
        if cit.get('state') and cit['state'] != grades['st'][row]:
            parked.append({'citation_id': cit.get('citation_id'),
                           'label': cit.get('mine_name'),
                           'reason': 'quote-state-mismatch', 'row': row})
            continue
        if not names_agree(cit.get('mine_name'), grades['name'][row]):
            parked.append({'citation_id': cit.get('citation_id'),
                           'label': cit.get('mine_name'),
                           'reason': 'quote-name-mismatch', 'row': row,
                           'row_name': grades['name'][row]})
            continue
        link = grades_link(row, 'citation_quote')
        if link is None:
            parked.append({'citation_id': cit.get('citation_id'),
                           'label': cit.get('mine_name'), 'reason': 'unlocated',
                           'row': row})
            continue
        citation_links.append({
            'citation_id': cit.get('citation_id'), 'doc_id': cit['doc_id'],
            'store_mine_id': cit.get('mine_id'),
            'mine_name': cit.get('mine_name'), 'page': cit.get('page'),
            'quote': cit.get('quote'), 'quote_located': cit.get('quote_located'),
            'link': link})

    subjects, labels = {}, {}
    for doc in manifest.get('documents', ()):
        for sub in doc.get('subjects', ()):
            subjects.setdefault(sub['mine_id'], sub)
            labels.setdefault(sub['mine_id'], sub.get('label') or sub['mine_id'])

    def rows_to_link(rows, method):
        """A set of candidate rows becomes a link only through one group."""
        reps = {canon[r] for r in rows}
        if len(reps) != 1:
            return 'ambiguous' if len(reps) > 1 else None
        rep = next(iter(reps))
        return grades_link(rep, method) if rep is not None else None

    links = {}
    order = ('evidence_name', 'subject_lookup')
    for sid in sorted(subjects):
        sub = subjects[sid]
        tiers = {}
        if sid.startswith('ws9-'):
            if sid in evidence:
                ev = evidence[sid]
                rows = by_name.get((resolve.normalise(ev['name']), ev['state']), ())
                if rows:
                    tiers['evidence_name'] = rows_to_link(rows, 'evidence_name')
            got = resolve.lookup(core_label(sub.get('label')), state=sub.get('state'))
            cands = got['candidates']
            if cands and not got['ambiguous'] and cands[0].get('located') \
                    and cands[0]['match'] == 'exact':
                row = resolve.parse_mine_id(cands[0]['mine_id'])
                tiers['subject_lookup'] = grades_link(row, 'subject_lookup')
        chosen = None
        for method in order:
            link = tiers.get(method)
            if link == 'ambiguous':
                parked.append({'store_mine_id': sid, 'label': labels[sid],
                               'reason': 'ambiguous:%s' % method})
                chosen = 'parked'
                break
            if link is None:
                continue
            others = {t['mine_id'] for m, t in tiers.items()
                      if isinstance(t, dict) and m != method}
            if others - {link['mine_id']}:
                parked.append({'store_mine_id': sid, 'label': labels[sid],
                               'reason': 'tier-conflict',
                               'mine_ids': sorted({link['mine_id']} | others)})
                chosen = 'parked'
                break
            chosen = link
            break
        if chosen == 'parked':
            continue
        if chosen is None and sid.startswith('stategeo'):
            site = stategeo.get(slug(sid))
            if site:
                chosen = {'kind': 'latlon', 'method': 'stategeo_site',
                          'front_end_id': site['front_end_id'], 'name': site['name'],
                          'lon': site['lon'], 'lat': site['lat'], 'located': True}
        if chosen is None:
            if any(tiers.get(m) is None and m in tiers for m in order):
                reason = 'unlocated'
            elif sid.startswith('district-') or sid.startswith('statewide-'):
                reason = 'district-not-a-mine'
            else:
                reason = 'no-buildable-reference'
            parked.append({'store_mine_id': sid, 'label': labels[sid],
                           'reason': reason})
            continue
        links[sid] = chosen

    group_index = {}
    for row, rep in canon.items():
        if rep is not None:
            group_index.setdefault('grades:%d' % rep, []).append(row)
    for rows in group_index.values():
        rows.sort()
    return {'links': links, 'citation_links': citation_links,
            'groups': group_index, 'parked': parked}


# ----------------------------------------------------------------- sections

def _fulltext(pages):
    """(text, page_starts) — pages joined so offsets map back to page numbers."""
    starts, parts, pos = [], [], 0
    for page in pages:
        starts.append(pos)
        parts.append(page)
        pos += len(page) + 2
    return '\n\n'.join(parts), starts


def _page_of(starts, offset):
    lo, hi = 0, len(starts) - 1
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if starts[mid] <= offset:
            lo = mid
        else:
            hi = mid - 1
    return lo + 1  # 1-based pdf page


def _label_pattern(cores):
    cores = sorted({c for c in cores if len(c) >= 3}, key=len, reverse=True)
    if not cores:
        return None
    return re.compile(r'\b(' + '|'.join(re.escape(c) for c in cores) + r')\b', re.I)


_LEADER = re.compile(r'[-_.·]{4,}')

#: verbs (and verb phrases) that describe development work being done —
#: "Plan and stope map of the Quartette mine" names workings; "the mine is
#: developed by two adits" describes them.  Only the latter keeps a window.
_DESCRIBES = re.compile(
    r'\b(driven|drove|sunk|sink|sinking|stoped|stoping|raised|extended|'
    r'opened|developed|breasted|drifting|excavated|explored\s+by|'
    r'consists?\s+of|comprises?|worked\s+(?:by|through)|'
    r'accessible\s+(?:by|through)|connect(?:s|ed)?\s+(?:by|with|the)|'
    r'run\s+(?:in|on)|crosscuts?\b.{0,24}\b(?:vein|lode|ore)|'
    r'(?:adit|tunnel|drift|crosscut|winze|raise|shaft|stope|incline)'
    r'\s+(?:was|were|is|are)\b|'
    # a measured working is a description even with no development verb:
    # "the shaft is 300 feet deep", "an adit 900 feet long"
    r'\b(?:is|was|are|were|of)\s+\d[\d,]*(?:\.\d+)?\s*'
    r'(?:feet|foot|ft\.?|m|metres?|meters?)\b|'
    r'\b\d[\d,]*(?:\.\d+)?\s*(?:feet|foot|ft\.?|m|metres?|meters?)\s+'
    r'(?:deep|long|below|in\s+depth|in\s+length)\b)',
    re.I)


def _descriptive(text):
    """Is this window prose about workings, not an index line about them?

    A table of contents or a list of figures names every mine; it describes
    none.  Keep a window only when a real sentence *describes* development —
    a mining noun plus a development verb, long enough to be prose, and not
    a leader-dotted index line.
    """
    for _, _, body in narrative.sentences(text):
        if _LEADER.search(body):
            continue
        if not narrative.MINING_WORDS.search(body) or not _DESCRIBES.search(body):
            continue
        if len(body.strip()) >= 30:
            return True
    return False


#: "<Proper Name> mine|shaft|..." — how a report mentions any mine by name.
#: Single spaces only: a match must not straddle a line break, or OCR'd
#: headings glue two unrelated lines into one "name".
_NAMED_MINE = re.compile(
    r'\b([A-Z][A-Za-z-]*(?:[ ][A-Z][A-Za-z-]*){0,2})[ ]'
    r'(?:mine|mines|shaft|claim|claims|lease|group|tunnel|property)\b')

#: leading words that are grammar, not name ("The Duplex mine", "See Good Hope")
_NAME_LEADING = {'the', 'a', 'an', 'see', 'this', 'that', 'these', 'those',
                 'near', 'at', 'of', 'from', 'to', 'and', 'or', 'by', 'in', 'on'}

#: capitalized sentence-starters that precede a working word without naming one
_NAME_STOPWORDS = {'the', 'this', 'that', 'a', 'no', 'his', 'her', 'their',
                   'main', 'new', 'old', 'north', 'south', 'east', 'west',
                   'upper', 'lower', 'deep', 'another', 'other', 'others',
                   'accessible', 'principal', 'several', 'many', 'each',
                   'every', 'some', 'all', 'most', 'such', 'both', 'either',
                   'neither', 'certain', 'various', 'numerous', 'adjacent',
                   'nearby', 'original', 'present', 'former', 'early',
                   'later', 'important', 'small', 'large', 'said', 'same',
                   'entire', 'whole', 'one', 'two', 'three', 'four', 'five',
                   'first', 'second', 'third'}


def _named_mines(text, target_cores):
    """Every '<Name> mine'-style phrase in the text that is not the target.

    A district report describes mines the store never registered as subjects;
    their names must still end a window, or their workings would be
    attributed to the mine being extracted.  A multi-word name counts on one
    sighting; a single-word name must recur, because a capitalized word at a
    sentence start ("Considerable stoping…") is usually not a name.  Only a
    name *equal* to the target is excluded — "Blue Jay Extension" must cut a
    "Blue Jay" window, and the longest-match scan in ``sections`` keeps the
    overlap from cutting the target's own name.
    """
    targets = {t.lower() for t in target_cores}
    counts = {}
    for m in _NAMED_MINE.finditer(text):
        words = m.group(1).split()
        while words and words[0].lower() in _NAME_LEADING:
            words = words[1:]
        if not words:
            continue
        core = ' '.join(words)
        low = core.lower()
        if low in _NAME_STOPWORDS or len(core) < 3:
            continue
        if low in targets:
            continue
        counts[core] = counts.get(core, 0) + 1
    return {core for core, n in counts.items()
            if (' ' in core or n >= 2)
            and (' ' in core or core.lower() not in _NAME_STOPWORDS)}


def sections(pages, target_cores, other_cores, quotes=(), max_chars=MAX_SECTION_CHARS):
    """Name-anchored windows of a document's text that are about one mine.

    ``target_cores`` are the mine's names (label core, bundle name core,
    citation names); ``other_cores`` are every *other* subject of the same
    document.  The first mention of another subject — or of any
    ``<Name> mine``-style phrase that is not the target — ends a window, so
    text about the next mine in the report is never attributed to this one.
    A located ``quote`` (from a reviewed citation) anchors a window even
    where the name does not appear.  Windows that never use mining language
    are dropped.

    Returns ``[{'text', 'pages', 'span'}]`` with 1-based pdf page numbers.
    """
    text, starts = _fulltext(pages)
    targets = {t for t in target_cores if t and len(t) >= 3}
    cuts = (set(other_cores) | _named_mines(text, targets)) - targets
    cuts = {c for c in cuts if len(c) >= 3}

    # One scan, longest name wins at a position: "Silver King" must classify
    # as Silver King wherever both "Silver King" and "King" are known names,
    # in either role — a target must not anchor inside another mine's name,
    # and a cut must not fire inside the target's own.
    anchors, cut_at = [], []
    scan_re = _label_pattern(targets | cuts)
    if scan_re:
        target_low = {t.lower() for t in targets}
        for m in scan_re.finditer(text):
            hit = re.sub(r'\s+', ' ', m.group(1)).lower()
            (anchors if hit in target_low else cut_at).append(m.start())

    for quote in quotes:
        toks = [re.escape(t) for t in str(quote or '').split()]
        if not toks:
            continue
        m = re.search(r'\s+'.join(toks), text)
        if m:
            anchors.append(m.start())

    spans = []
    for at in sorted(anchors):
        lo = text.rfind('\n', 0, at) + 1
        hi = min(len(text), at + max_chars)
        nxt = min((c for c in cut_at if c > at), default=None)
        if nxt is not None:
            hi = min(hi, nxt)
        if hi > lo:
            spans.append((lo, hi))

    merged = []
    for lo, hi in sorted(spans):
        if merged and lo <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(hi, merged[-1][1]))
        else:
            merged.append((lo, hi))

    out = []
    for lo, hi in merged:
        body = text[lo:hi]
        if not _descriptive(body):
            continue
        out.append({'text': body,
                    'pages': sorted({_page_of(starts, lo), _page_of(starts, max(lo, hi - 1))}),
                    'span': [lo, hi]})
    return out


# -------------------------------------------------------------- assignments

def assignments(manifest=None, bridges=None, docs_filter=None, log=print,
                store_root=None, cache_dir=None):
    """The autopopulator's work list: one unit per buildable mine.

    ``{'units': [...], 'parked': [...], 'stats': {...}}`` where a unit is::

        {'key': 'grades:17' | 'stategeo:IGS DD-1 IF0126',
         'site_kind': 'grades' | 'latlon',
         'site_ref': <link from build_bridges>,
         'label': 'White Caps mine',
         'store_mine_ids': ['ws9-nv-white-caps'],
         'methods': ['citation_quote'],
         'texts': [{'doc_id', 'title', 'source_url', 'publication_year',
                    'pages', 'text', 'citation_pages'}],
         'documents': [{doc metadata}]}

    A mine that bridges but yields no descriptive section is returned with
    ``texts: []`` so the ledger can say "documents, but nothing to build".
    """
    manifest = manifest if manifest is not None else load_manifest()
    bridged = bridges if bridges is not None else build_bridges(manifest)
    links = bridged['links']

    docs = [d for d in manifest.get('documents', ())
            if docs_filter is None or d['doc_id'] in docs_filter]
    doc_ids = {d['doc_id'] for d in docs}
    cite_links_by_doc = {}
    for cl in bridged['citation_links']:
        if cl['doc_id'] in doc_ids:
            cite_links_by_doc.setdefault(cl['doc_id'], []).append(cl)

    by_key = {}

    def unit_for(key, link, label):
        unit = by_key.setdefault(key, {
            'key': key, 'site_kind': link['kind'], 'site_ref': link,
            'label': link.get('name') or label,
            'grade_rows': link.get('rows', []),
            'store_mine_ids': [], 'methods': [], 'texts': [], 'documents': []})
        if link['method'] not in unit['methods']:
            unit['methods'].append(link['method'])
        return unit

    def add_doc(unit, doc, cited_pages, n_sections):
        for rec in unit['documents']:
            if rec['doc_id'] == doc['doc_id']:
                rec['cited_pages'] = sorted(set(rec['cited_pages']) | set(cited_pages))
                rec['sections'] += n_sections
                return
        unit['documents'].append({
            'doc_id': doc['doc_id'], 'title': doc.get('title'),
            'source_url': doc.get('source_url'),
            'catalog_url': doc.get('catalog_url'),
            'publication_year': doc.get('publication_year'),
            'citation': doc.get('citation'), 'pages': doc.get('pages'),
            'cited_pages': sorted(cited_pages), 'sections': n_sections})

    def add_sections(unit, doc, got, cited_pages):
        for sec in got:
            span = [sec['span'][0], sec['span'][1]]
            if any(t['doc_id'] == doc['doc_id'] and t['span'] == span
                   for t in unit['texts']):
                continue
            unit['texts'].append({
                'doc_id': doc['doc_id'], 'title': doc.get('title'),
                'source_url': doc.get('source_url'),
                'publication_year': doc.get('publication_year'),
                'pages': sec['pages'], 'span': span, 'text': sec['text'],
                'citation_pages': sorted(cited_pages)})

    for doc in docs:
        doc_subjects = {s['mine_id']: s for s in doc.get('subjects', ())}
        doc_cite_links = cite_links_by_doc.get(doc['doc_id'], ())
        subject_links = {sid: links[sid] for sid in doc_subjects if sid in links}
        if not subject_links and not doc_cite_links:
            continue
        try:
            pages = page_texts(doc, store_root=store_root, cache_dir=cache_dir)
        except SystemExit:
            raise
        except Exception as exc:
            log('skipping %s: %s' % (doc['doc_id'][:12], exc))
            continue
        all_cores = {sid: core_label(sub.get('label'))
                     for sid, sub in doc_subjects.items()}
        for cl in doc_cite_links:
            all_cores.setdefault('cite:' + (cl.get('citation_id') or ''),
                                 core_label(cl.get('mine_name')))

        # citation-tier: each quote-joined citation is its own mine
        by_canon = {}
        for cl in doc_cite_links:
            by_canon.setdefault(cl['link']['mine_id'], []).append(cl)
        for key, cls in sorted(by_canon.items()):
            link = cls[0]['link']
            unit = unit_for(key, link, cls[0].get('mine_name'))
            for cl in cls:
                sid = cl.get('store_mine_id')
                if sid and sid not in unit['store_mine_ids']:
                    unit['store_mine_ids'].append(sid)
            targets = {core_label(c.get('mine_name')) for c in cls}
            targets.add(core_label(link.get('name')))
            targets = {t for t in targets if t}
            others = {c for c in all_cores.values() if c} - targets
            got = sections(pages, targets, others,
                           quotes=[c.get('quote') for c in cls
                                   if c.get('quote_located')])
            cited = {c['page'] for c in cls if c.get('page') is not None}
            add_doc(unit, doc, cited, len(got))
            add_sections(unit, doc, got, cited)

        # subject-tier: mines the document lists as subjects
        for sid, link in sorted(subject_links.items()):
            key = link['mine_id'] if link['kind'] == 'grades' else link['front_end_id']
            if link['kind'] == 'grades' and key in by_canon:
                unit = by_key[key]
                if sid not in unit['store_mine_ids']:
                    unit['store_mine_ids'].append(sid)
                if link['method'] not in unit['methods']:
                    unit['methods'].append(link['method'])
                continue  # citations already carved this doc for this mine
            unit = unit_for(key, link, doc_subjects[sid].get('label'))
            if sid not in unit['store_mine_ids']:
                unit['store_mine_ids'].append(sid)
            targets = {all_cores[sid], core_label(link.get('name'))}
            targets = {t for t in targets if t}
            others = {c for c in all_cores.values() if c} - targets
            got = sections(pages, targets, others)
            add_doc(unit, doc, set(), len(got))
            add_sections(unit, doc, got, set())

    units = sorted(by_key.values(), key=lambda u: u['key'])
    stats = {
        'documents': len(docs),
        'linked_store_ids': len(links),
        'citation_links': len(bridged['citation_links']),
        'parked': len(bridged['parked']),
        'mines': len(units),
        'mines_with_text': sum(1 for u in units if u['texts']),
        'sections': sum(len(u['texts']) for u in units),
    }
    return {'units': units, 'parked': bridged['parked'], 'stats': stats}


if __name__ == '__main__':  # pragma: no cover - convenience report
    got = assignments()
    print(json.dumps(got['stats'], indent=2))
    for unit in got['units']:
        print('%-28s %-10s %s docs=%d sections=%d' % (
            unit['key'], ','.join(unit['methods']), (unit['label'] or '')[:32],
            len(unit['documents']), len(unit['texts'])))
