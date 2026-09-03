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
4. **Site index** — for the WS13 corpus, whose documents name mines in
   their own namespaces (AZGS ``ADMM-…`` codes, bare IGS codes, names,
   states and counties), ``SiteIndex`` resolves a ``ws13_documents``-shaped
   row to a *located* front-end site (``grades:``, ``stategeo:``, ``mrds:``,
   ``usmin:``, ``ardf:``) through five recorded tiers, strongest first:
   a verified identity row of ``ws13_mine_id_map``, an embedded survey code,
   exact name + state + county, exact name + state, and — for district or
   county files — a park that lists the mines the text names so the driver
   can carve one section per mine (``targets_for``).  A tier that yields
   several physical mines parks the document with every candidate listed;
   fuzzy matches are reported and never resolved.

Unlike ``pipelines/geomodel/`` (stdlib-pure by policy), this module may use
PyMuPDF — the same dependency ``build_doc_store.py`` already requires.
"""

import difflib
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


# ---------------------------------------------------- the located-site index
#
# The WS13 corpus (``ws13_documents``) names mines in its own namespaces —
# AZGS ``ADMM-…`` collection ids, bare IGS survey codes (``IF0126``), names,
# states and counties — and ``ws13_mine_id_map`` bridges *some* of them to
# the front end's ids.  The modeller needs the other direction: given one
# document row, which **located** site does it describe?  ``SiteIndex`` holds
# every located site the map knows, keyed the way the map keys them, and
# ``SiteIndex.resolve`` walks five tiers, strongest first.  Its one rule is
# the module's: the first tier that yields exactly one physical mine wins;
# a tier that yields several parks the document with every candidate listed;
# nothing is ever picked by score.

#: front-end namespaces, in the order a merged physical mine keeps its key
#: (a graded mine's card is where the model is shown; the survey record is
#: the next most specific; MRDS is a legacy dump; USMIN is a map symbol;
#: ARDF is Alaska's occurrence file)
SITE_KINDS = ('grades', 'stategeo', 'mrds', 'usmin', 'ardf')
_KIND_RANK = dict((k, i) for i, k in enumerate(SITE_KINDS))

#: two same-named located sites in one state closer than this are one hole
#: in the ground recorded twice; farther apart they are two mines, whatever
#: the names say
SAME_MINE_KM = 2.0

#: the fuzzy pass reports near-misses at or above this ratio.  It resolves
#: nothing — a near-miss is a question for a person, never an answer.
FUZZY_CUTOFF = 0.85
FUZZY_LIMIT = 8

#: the status a namespace reports when nothing on disk feeds it
NO_SOURCE = 'no source on this machine'

#: per-tier confidence, mirroring ws13_mine_id_map's bands: the exact tiers
#: sit above the retrieval path's 0.8 admission line and the fuzzy pass
#: never produces a candidate at all
CONF_IDENTITY_ROW = 1.0
CONF_EMBEDDED_CODE = 0.95
CONF_NAME_STATE_COUNTY = 0.9
CONF_NAME_STATE = 0.85

#: where an ARDF site list may live, tried in order.  The first is the hook
#: — a columnar ``ardf_ak.json`` shaped like the other site files (id/nm/x/y,
#: optional ``d`` district and ``work`` workings text) — which no build has
#: written yet.  The second is the Alaska grade-evidence crosswalk that *is*
#: on disk: a reviewed subset (21 targets), so its coverage is reported as
#: partial.  The full ARDF is only on this machine as PMTiles
#: (``site/data/tiles/national/ardf.pmtiles``), which needs tippecanoe to
#: decode and is therefore not read here.
ARDF_SOURCES = (
    (os.path.join('build-inputs', 'data', 'sites', 'ardf_ak.json'), 'columnar', 'full'),
    (os.path.join('grades-research', 'ak', 'ardf_target_crosswalk.json'), 'records',
     'partial: reviewed target crosswalk, not the full ARDF'),
)

_STATE_CODES = {
    'alabama': 'AL', 'alaska': 'AK', 'arizona': 'AZ', 'arkansas': 'AR',
    'california': 'CA', 'colorado': 'CO', 'connecticut': 'CT', 'delaware': 'DE',
    'district of columbia': 'DC', 'florida': 'FL', 'georgia': 'GA', 'hawaii': 'HI',
    'idaho': 'ID', 'illinois': 'IL', 'indiana': 'IN', 'iowa': 'IA', 'kansas': 'KS',
    'kentucky': 'KY', 'louisiana': 'LA', 'maine': 'ME', 'maryland': 'MD',
    'massachusetts': 'MA', 'michigan': 'MI', 'minnesota': 'MN', 'mississippi': 'MS',
    'missouri': 'MO', 'montana': 'MT', 'nebraska': 'NE', 'nevada': 'NV',
    'new hampshire': 'NH', 'new jersey': 'NJ', 'new mexico': 'NM', 'new york': 'NY',
    'north carolina': 'NC', 'north dakota': 'ND', 'ohio': 'OH', 'oklahoma': 'OK',
    'oregon': 'OR', 'pennsylvania': 'PA', 'rhode island': 'RI', 'south carolina': 'SC',
    'south dakota': 'SD', 'tennessee': 'TN', 'texas': 'TX', 'utah': 'UT',
    'vermont': 'VT', 'virginia': 'VA', 'washington': 'WA', 'west virginia': 'WV',
    'wisconsin': 'WI', 'wyoming': 'WY',
}

_NS_ID = re.compile(r'^(grades|stategeo|mrds|usmin|ardf)([:-])(.+)$', re.I)
_CODE_SHAPE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{3,23}$')


def _state_code(value):
    """'NV' / 'nv' / 'Nevada' -> 'NV'; anything else -> None."""
    s = re.sub(r'\s+', ' ', str(value or '').strip())
    if not s:
        return None
    if len(s) == 2 and s.isalpha():
        return s.upper()
    return _STATE_CODES.get(s.lower())


def _county_key(value):
    """' Maricopa County' -> 'maricopa'; 'Mohave (AZ) County' -> 'mohave'.

    The corpus and the site files spell counties differently and neither is
    wrong; this folds both to the bare name (the same rule as
    ``ws13_mine_id_map.normalize_county``)."""
    text = re.sub(r'\([^)]*\)', ' ', str(value or '').lower())
    text = re.sub(r'[^a-z0-9]+', ' ', text).strip()
    if text.endswith(' county'):
        text = text[:-len(' county')].strip()
    return text


def _county_keys(value):
    """Every county a source's county string names ('Pima, Santa Cruz')."""
    return frozenset(k for k in (_county_key(p) for p in str(value or '').split(','))
                     if k)


def _aliases(name):
    """The names one site record carries.

    Survey files list synonyms in one field — IGS 'North Star Mine, McCarty
    Group', MBMG 'DJ SILVER / SAINT LOUIS MINE', ARDF 'Miners River; Miners
    Bay' — and a document may use any of them.  The trailing parenthetical
    (a grades-bundle qualifier such as '(mill ore)') is not a name."""
    x = re.sub(r'\s*\(.*\)\s*$', '', str(name or ''))
    parts = re.split(r'\s*(?:;|,|\s/\s)\s*', x)
    return [p.strip() for p in parts if p and p.strip()]


def _looks_like_code(token):
    """Survey-code shaped: 4-24 chars, carries a digit, is not digits only."""
    t = str(token or '').strip()
    return (bool(_CODE_SHAPE.match(t)) and any(ch.isdigit() for ch in t)
            and not t.isdigit())


def _codes_of(record_id):
    """The embedded codes a corpus ``mine_ids`` entry could carry for a site.

    'IGS DD-1 IF0126' -> ['IGS DD-1 IF0126', 'IF0126']: the whole id and its
    last segment, when code-shaped.  Digit-only segments are never codes —
    MRDS dep_ids and NBMG district file numbers are both bare integers and
    52 of them collide (ws13_mine_id_map's NUMERIC_NAMESPACES_BLOCKED)."""
    rid = str(record_id or '').strip()
    out = []
    if _looks_like_code(rid):
        out.append(rid.upper())
    last = re.split(r'[\s_-]+', rid)[-1] if rid else ''
    if last != rid and _looks_like_code(last):
        out.append(last.upper())
    return out


class _StateTable(object):
    """Every located site of one state, indexed the ways a document can name it."""

    def __init__(self, state):
        self.state = state
        self.sites = {}            # mine_key -> site
        self.alias = {}            # non-canonical grades row key -> canonical key
        self.by_slug = {}          # 'stategeo-igs-dd-1-if0126' -> mine_key
        self.by_bare = {}          # record id as the source spells it -> [mine_key]
        self.by_name = {}          # _mild(alias) -> [mine_key]   (located only)
        self.by_name_unlocated = {}
        self.by_code = {}          # embedded code (upper) -> [mine_key]
        self.tokens = {}           # name token -> set(name key)   (fuzzy prefilter)
        self.prefixes = {}         # first 4 chars of a token -> set(name key)
        self.sources = {}          # kind -> status string

    def register(self, site):
        key = site['mine_key']
        if key in self.sites:
            return False
        self.sites[key] = site
        self.by_slug[slug(key)] = key
        self.by_bare.setdefault(site['id'], []).append(key)
        for code in _codes_of(site['id']):
            self.by_code.setdefault(code, []).append(key)
        names = self.by_name if site['located'] else self.by_name_unlocated
        for k in site['keys']:
            names.setdefault(k, []).append(key)
            if site['located']:
                for tok in k.split():
                    if len(tok) >= 3:
                        self.tokens.setdefault(tok, set()).add(k)
                        self.prefixes.setdefault(tok[:4], set()).add(k)
        return True


def _site(kind, rid, name, lon, lat, state, county=None, district=None, typ=None,
          rows=None, source=None, extra=None):
    aliases = _aliases(name)
    keys = []
    for a in aliases:
        k = _mild(a)
        if k and k not in keys:
            keys.append(k)
    site = {
        'mine_key': '%s:%s' % (kind, rid), 'kind': kind, 'id': str(rid),
        'name': name, 'aliases': aliases, 'keys': keys,
        'lon': lon, 'lat': lat, 'located': lon is not None and lat is not None,
        'state': state, 'county': county or None, 'counties': _county_keys(county),
        'district': district or None, 'type': typ or None, 'source': source,
    }
    if rows is not None:
        site['rows'] = rows
    if extra:
        site.update(extra)
    return site


_SITE_INDEXES = {}


class SiteIndex(object):
    """Every located site the front end knows, resolved per document.

    Built lazily per state and cached: resolving a Nevada document loads
    ``grades.json`` (once, it is cross-state) and Nevada's ``stategeo_nv``,
    ``mrds_nv`` and ``usmin_nv`` files — never Idaho's.  ``coverage(state)``
    says, per namespace, what fed the index on this machine.
    """

    def __init__(self, root=None, grades_path=None, sites_dir=None, ardf_paths=None):
        self.root = os.path.abspath(root) if root else ROOT
        self.grades_path = grades_path or (
            os.path.join(self.root, 'site', 'data', 'grades', 'grades.json')
            if root else resolve.GRADES)
        self.sites_dir = sites_dir or (
            os.path.join(self.root, 'build-inputs', 'data', 'sites') if root else SITES_DIR)
        self.ardf_paths = tuple(ardf_paths) if ardf_paths is not None else ARDF_SOURCES
        self._grades = None
        self._tables = {}

    @classmethod
    def load(cls, root=None, **kw):
        """The process-wide index for a root: built once, states on demand."""
        key = (os.path.abspath(root) if root else ROOT,
               tuple(sorted((k, str(v)) for k, v in kw.items())))
        if key not in _SITE_INDEXES:
            _SITE_INDEXES[key] = cls(root, **kw)
        return _SITE_INDEXES[key]

    # ------------------------------------------------------------ loading
    def _grades_bundle(self):
        if self._grades is None:
            if os.path.exists(self.grades_path):
                bundle = _load_grades(self.grades_path)
                groups, canon = _grade_groups(bundle)
                by_state = {}
                for i in range(bundle['n']):
                    by_state.setdefault(_state_code(bundle['st'][i]) or '', []).append(i)
                self._grades = {'bundle': bundle, 'groups': groups, 'canon': canon,
                                'by_state': by_state,
                                'status': 'loaded: %d rows' % bundle['n']}
            else:
                self._grades = {'bundle': None, 'groups': {}, 'canon': {},
                                'by_state': {}, 'status': NO_SOURCE}
        return self._grades

    def loaded_states(self):
        """States whose tables have been built so far (a laziness check)."""
        return sorted(self._tables)

    def states_available(self):
        """Every state some site source on this machine covers."""
        out = set()
        for path in glob.glob(os.path.join(self.sites_dir, '*_??.json')):
            m = re.match(r'^(stategeo|mrds|usmin|ardf)_([a-z]{2})\.json$',
                         os.path.basename(path))
            if m:
                out.add(m.group(2).upper())
        out |= set(self._grades_bundle()['by_state']) - {''}
        for rel, _, _ in self.ardf_paths:
            if os.path.exists(rel if os.path.isabs(rel) else os.path.join(self.root, rel)):
                out.add('AK')
        return sorted(out)

    def state(self, st):
        code = _state_code(st)
        if code is None:
            raise ValueError('not a state: %r' % (st,))
        if code not in self._tables:
            table = _StateTable(code)
            self._add_grades(table)
            for kind in ('stategeo', 'mrds', 'usmin'):
                self._add_columnar(table, kind)
            self._add_ardf(table)
            self._tables[code] = table
        return self._tables[code]

    def coverage(self, st):
        """Per namespace, what fed this state's index on this machine."""
        return dict(self.state(st).sources)

    def _add_grades(self, table):
        g = self._grades_bundle()
        b = g['bundle']
        if b is None:
            table.sources['grades'] = NO_SOURCE
            return
        canon, groups = g['canon'], g['groups']
        rows = g['by_state'].get(table.state, [])
        source = os.path.relpath(self.grades_path, self.root)
        cnty = b.get('cnty') or [None] * b['n']
        dist = b.get('dist') or [None] * b['n']
        located = 0
        for i in rows:
            rep = canon.get(i)
            key = 'grades:%d' % i
            if rep is None:
                table.register(_site('grades', i, b['name'][i], None, None, table.state,
                                     cnty[i], dist[i], rows=[i], source=source))
                continue
            if rep != i:
                table.alias[key] = 'grades:%d' % rep
                continue
            group = sorted(groups.get(rep, [i]))
            county = cnty[i] or next((cnty[r] for r in group if cnty[r]), None)
            site = _site('grades', i, b['name'][i], b['x'][i], b['y'][i], table.state,
                         county, dist[i], rows=group, source=source)
            # every row of the group names the same mine ("Victoria mine
            # (shipping ore)" and "Victoria mine (1,050-foot level)")
            for r in group:
                for a in _aliases(b['name'][r]):
                    k = _mild(a)
                    if a not in site['aliases']:
                        site['aliases'].append(a)
                    if k and k not in site['keys']:
                        site['keys'].append(k)
            site['counties'] = frozenset(c for r in group for c in _county_keys(cnty[r]))
            table.register(site)
            located += 1
        table.sources['grades'] = ('loaded: %d located mines (%d rows) in %s from %s'
                                   % (located, len(rows), table.state, source))

    def _add_columnar(self, table, kind):
        name = '%s_%s.json' % (kind, table.state.lower())
        path = os.path.join(self.sites_dir, name)
        if not os.path.exists(path):
            table.sources[kind] = 'no %s on this machine' % name
            return
        with open(path, encoding='utf-8') as fh:
            data = json.load(fh)
        n_all = int(data.get('n') or len(data.get('x') or []))
        ids = data.get('id') or []
        if not ids:
            # USMIN publishes points and feature types without record ids or
            # names (the national tiles mint a fid the columnar file never
            # carries), so nothing in it can be matched by name or by id here.
            table.sources[kind] = (
                '%s carries %d points and %d feature types but no record ids or '
                'names: nothing in it can be matched by name or fid on this machine'
                % (name, n_all, len(data.get('types') or [])))
            return
        self._index_columnar(table, kind, data, name)

    def _index_columnar(self, table, kind, data, source):
        ids = data.get('id') or []
        n = len(ids)
        names = data.get('nm') or [None] * n
        xs, ys = data.get('x') or [None] * n, data.get('y') or [None] * n
        counties = data.get('county') or [None] * n
        districts = data.get('d') or [None] * n
        types, tcol = data.get('types'), data.get('t')
        tys = data.get('ty') or [None] * n
        works = data.get('work') or [None] * n
        located = dupes = 0
        for i, rid in enumerate(ids):
            rid = str(rid or '').strip()
            if not rid:
                continue
            typ = None
            if types and tcol and i < len(tcol) and isinstance(tcol[i], int) \
                    and 0 <= tcol[i] < len(types):
                typ = types[tcol[i]]
            elif i < len(tys) and tys[i]:
                typ = tys[i]
            extra = {'work': works[i]} if i < len(works) and works[i] else None
            site = _site(kind, rid, names[i] if i < len(names) else None,
                         xs[i] if i < len(xs) else None, ys[i] if i < len(ys) else None,
                         table.state, counties[i] if i < len(counties) else None,
                         districts[i] if i < len(districts) else None, typ,
                         source=source, extra=extra)
            if table.register(site):
                located += 1 if site['located'] else 0
            else:
                dupes += 1
        table.sources[kind] = 'loaded: %d sites (%d located%s) from %s' % (
            n, located, ', %d duplicate ids ignored' % dupes if dupes else '', source)

    def _add_ardf(self, table):
        if table.state != 'AK':
            table.sources['ardf'] = 'ARDF is Alaska-only; not consulted for %s' % table.state
            return
        for rel, form, coverage in self.ardf_paths:
            path = rel if os.path.isabs(rel) else os.path.join(self.root, rel)
            if not os.path.exists(path):
                continue
            with open(path, encoding='utf-8') as fh:
                data = json.load(fh)
            if form == 'columnar':
                self._index_columnar(table, 'ardf', data, rel)
            else:
                n = 0
                for rec in data.get('records') or ():
                    rid = str(rec.get('ardf_no') or '').strip()
                    if not rid:
                        continue
                    extra = {'work': rec['work']} if rec.get('work') else None
                    if table.register(_site('ardf', rid, rec.get('site'),
                                            rec.get('longitude'), rec.get('latitude'),
                                            'AK', None, rec.get('district'),
                                            rec.get('commodities_main'), source=rel,
                                            extra=extra)):
                        n += 1
                table.sources['ardf'] = 'loaded: %d sites from %s (%s)' % (n, rel, coverage)
            if form == 'columnar':
                table.sources['ardf'] += ' (%s)' % coverage
            return
        table.sources['ardf'] = NO_SOURCE

    # ------------------------------------------------------------ lookups
    def _get_in(self, table, key):
        key = table.alias.get(key, key)
        return table.sites.get(key)

    def find_id(self, front_end_id, state=None, scan=False):
        """Sites a front-end id names, in any of the three spellings the map
        uses ('stategeo:IGS DD-1 IF0126', 'stategeo-igs-dd-1-if0126', bare).

        Returns ``(sites, note)``.  The document's state is probed first; a
        miss scans the other states only when ``scan`` is set (a verified
        identity row is worth the cost, a code guess is not) or when the
        document has no state at all.
        """
        fid = str(front_end_id or '').strip()
        if not fid:
            return [], 'empty id'
        m = _NS_ID.match(fid)
        kind = m.group(1).lower() if m else None
        if kind == 'grades' and m.group(2) == ':' and m.group(3).isdigit():
            g = self._grades_bundle()
            row = int(m.group(3))
            if g['bundle'] is None or not 0 <= row < g['bundle']['n']:
                return [], '%s: not a row of the grades bundle' % fid
            st = _state_code(g['bundle']['st'][row])
            if st is None:
                return [], '%s: the bundle row has no state' % fid
            site = self._get_in(self.state(st), 'grades:%d' % row)
            note = 'grades row %d is in %s' % (row, st)
            if state and st != state:
                note += ', the document says %s' % state
            return ([site] if site else []), note

        def probe(table):
            if kind and m.group(2) == ':':
                site = self._get_in(table, '%s:%s' % (kind, m.group(3)))
                return [site] if site else []
            if kind:
                key = table.by_slug.get(slug(fid))
                return [table.sites[key]] if key else []
            return [self._get_in(table, k) for k in table.by_bare.get(fid, ())]

        tried = []
        if state:
            hits = probe(self.state(state))
            tried.append(state)
            if hits:
                return hits, 'found in %s' % state
        if scan or not state:
            for st in self.states_available():
                if st in tried:
                    continue
                hits = probe(self.state(st))
                tried.append(st)
                if hits:
                    return hits, ('found in %s after scanning %s' % (st, ', '.join(tried))
                                  if state else 'found in %s (no state on the document)' % st)
        return [], 'not in the index (%s searched)' % ', '.join(tried)

    def get(self, mine_key, state=None):
        """One site by its front-end key, or ``None``."""
        sites, _ = self.find_id(mine_key, state, scan=True)
        return sites[0] if sites else None

    def _by_code(self, code, state):
        code = str(code).strip().upper()
        if state:
            tables = [self.state(state)]
        else:
            tables = [self.state(st) for st in self.states_available()]
        out = []
        for t in tables:
            out.extend(t.sites[k] for k in t.by_code.get(code, ()))
        return out

    def _siblings(self, site):
        """Same-named located sites of the same state within SAME_MINE_KM."""
        if not site['located']:
            return []
        table = self.state(site['state'])
        out = []
        for k in site['keys']:
            for key in table.by_name.get(k, ()):
                other = table.sites[key]
                if other is site or other in out:
                    continue
                if resolve.distance_km(site['lon'], site['lat'],
                                       other['lon'], other['lat']) <= SAME_MINE_KM:
                    out.append(other)
        return out

    def lookup_name(self, name, state=None, county=None):
        """Every located site whose core name equals the query's.

        ``county`` restricts to sites that *record* a matching county;
        same-named sites that record no county are returned separately under
        ``county_unrecorded`` (never silently dropped, never silently
        admitted).  No state means every state on this machine is searched
        and the result says so.
        """
        key = _mild(name)
        cty = _county_key(county) or None
        out = {'name': name, 'key': key, 'state': state, 'county': cty,
               'sites': [], 'county_unrecorded': [], 'county_mismatch': [],
               'unlocated': [], 'states_searched': []}
        if not key:
            return out
        states = [state] if state else self.states_available()
        out['states_searched'] = states
        for st in states:
            table = self.state(st)
            for mk in table.by_name.get(key, ()):
                site = table.sites[mk]
                if cty:
                    if not site['counties']:
                        out['county_unrecorded'].append(site)
                        continue
                    if cty not in site['counties']:
                        out['county_mismatch'].append(site)
                        continue
                out['sites'].append(site)
            out['unlocated'].extend(table.sites[mk] for mk in table.by_name_unlocated.get(key, ()))
        return out

    # ------------------------------------------------------- physical mines
    def _cluster(self, sites, same_name=True):
        """Group located sites into physical mines.

        Two sites are one mine when they are in the same state, within
        ``SAME_MINE_KM``, and — unless ``same_name`` is off, which only the
        verified-identity tier does — share a normalised name.  Order is
        deterministic: the preferred namespace, then the key."""
        ordered = sorted((s for s in sites if s['located']),
                         key=lambda s: (_KIND_RANK[s['kind']], s['mine_key']))
        clusters = []
        for s in ordered:
            for c in clusters:
                head = c[0]
                if head['state'] != s['state']:
                    continue
                if same_name and not (set(head['keys']) & set(s['keys'])):
                    continue
                if resolve.distance_km(head['lon'], head['lat'], s['lon'], s['lat']) <= SAME_MINE_KM:
                    c.append(s)
                    break
            else:
                clusters.append([s])
        return clusters

    def _extend(self, clusters):
        """Pull each cluster's same-named neighbours in from the other namespaces."""
        out = []
        for c in clusters:
            keys = {s['mine_key'] for s in c}
            ext = list(c)
            for s in c:
                for sib in self._siblings(s):
                    if sib['mine_key'] not in keys:
                        ext.append(sib)
                        keys.add(sib['mine_key'])
            out.append(sorted(ext, key=lambda s: (_KIND_RANK[s['kind']], s['mine_key'])))
        return out

    def _candidate(self, cluster, method, confidence, evidence):
        head = cluster[0]
        merged = []
        for s in cluster[1:]:
            merged.append({'mine_key': s['mine_key'], 'kind': s['kind'], 'name': s['name'],
                           'distance_km': round(resolve.distance_km(
                               head['lon'], head['lat'], s['lon'], s['lat']), 3)})
        ev = dict(evidence)
        ev['merged'] = merged
        ev['summary'] = '%s; state %s; county %s; %s' % (
            ev.get('matched_summary', 'key %s' % head['mine_key']),
            head['state'],
            ("'%s' = '%s'" % (ev['county'], head['county'])) if ev.get('county')
            else ('not used' if not head['county'] else 'site records %r, document none' % head['county']),
            ('merged ' + ', '.join('%s (%.3f km)' % (m['mine_key'], m['distance_km'])
                                   for m in merged)) if merged else 'no merge')
        aliases = list(head['aliases'])
        for s in cluster[1:]:
            for a in s['aliases']:
                if a not in aliases:
                    aliases.append(a)
        return {
            'mine_key': head['mine_key'], 'kind': head['kind'], 'name': head['name'],
            'aliases': aliases, 'lon': head['lon'], 'lat': head['lat'],
            'state': head['state'], 'county': head['county'], 'district': head['district'],
            'type': head['type'], 'rows': head.get('rows'),
            'also': [dict(m) for m in merged],
            'method': method, 'confidence': confidence, 'evidence': ev,
        }

    # ---------------------------------------------------------- name tiers
    def _name_tier(self, names, state, county):
        """Clusters for a list of document names at one tier (county or not)."""
        per_name, sites, seen = {}, [], set()
        for nm in names:
            got = self.lookup_name(nm, state, county)
            hits = list(got['sites'])
            # a same-named site within 2 km that records no county is the same
            # hole in the ground, county or not
            for s in list(hits):
                for sib in self._siblings(s):
                    if sib not in hits:
                        hits.append(sib)
            per_name[nm] = {
                'key': got['key'],
                'matched': [s['mine_key'] for s in hits],
                'county_unrecorded': [s['mine_key'] for s in got['county_unrecorded']
                                      if s not in hits],
                'county_mismatch': [s['mine_key'] for s in got['county_mismatch']],
                'unlocated': [s['mine_key'] for s in got['unlocated']],
                'states_searched': got['states_searched'],
            }
            for s in hits:
                if s['mine_key'] not in seen:
                    seen.add(s['mine_key'])
                    sites.append(s)
        return self._extend(self._cluster(sites)), per_name

    def _name_candidates(self, clusters, per_name, method, confidence, state, county):
        cands = []
        for c in clusters:
            keys = {s['mine_key'] for s in c}
            names = [nm for nm, info in per_name.items() if keys & set(info['matched'])]
            matched = []
            for s in c:
                for nm in names:
                    k = per_name[nm]['key']
                    alias = next((a for a in s['aliases'] if _mild(a) == k), None)
                    if alias is not None:
                        matched.append({'mine_key': s['mine_key'], 'alias': alias,
                                        'normalised': k, 'county': s['county']})
            searched = per_name[names[0]]['states_searched'] if names else []
            ev = {
                'names': names, 'normalised': sorted({per_name[n]['key'] for n in names}),
                'state': state or 'none on the document (%s searched)' % ', '.join(searched),
                'county': county, 'matched': matched,
                'matched_summary': '; '.join(
                    "'%s' -> '%s' = '%s' (%s)" % (nm, per_name[nm]['key'], m['alias'], m['mine_key'])
                    for nm in names for m in matched if m['normalised'] == per_name[nm]['key']),
                'county_unrecorded': sorted({k for nm in names
                                             for k in per_name[nm]['county_unrecorded']}),
            }
            cands.append(self._candidate(c, method, confidence, ev))
        return cands

    def _fuzzy(self, names, state):
        """Near-misses for the report.  Never a candidate."""
        states = [state] if state else self.states_available()
        near = []
        for nm in names:
            q = _mild(nm)
            if not q:
                continue
            pool = set()
            for st in states:
                table = self.state(st)
                for tok in q.split():
                    if len(tok) < 3:
                        continue
                    # a shared token, or a shared 4-letter stem ('bluebirds'
                    # beside 'bluebird'): the prefilter only bounds the
                    # ratio sweep, the cutoff below decides
                    pool |= {(st, k) for k in table.tokens.get(tok, ())}
                    pool |= {(st, k) for k in table.prefixes.get(tok[:4], ())}
            scored = []
            for st, k in pool:
                if k == q:
                    continue
                r = difflib.SequenceMatcher(None, q, k).ratio()
                if r >= FUZZY_CUTOFF:
                    scored.append((round(r, 4), st, k))
            scored.sort(key=lambda x: (-x[0], x[1], x[2]))
            for r, st, k in scored[:FUZZY_LIMIT]:
                near.append({'name': nm, 'near': k, 'ratio': r, 'state': st,
                             'mine_keys': list(self.state(st).by_name.get(k, ()))})
        return near

    # ------------------------------------------------------------- resolve
    def resolve_names(self, names, state, county=None):
        """Each name on its own: ``{name: {'status', 'candidates', 'tier', ...}}``.

        The per-mine half of a district report: the driver hands in the
        names ``_named_mines`` found in the text and carves one section per
        name that resolves to exactly one physical mine.  ``status`` is
        ``resolved``, ``ambiguous`` (candidates listed, never picked) or
        ``unmatched`` (near-misses listed under ``near``, never resolved)."""
        st = _state_code(state)
        cty = _county_key(county) or None
        out = {}
        for nm in names:
            item = {'name': nm, 'key': _mild(nm), 'status': 'unmatched',
                    'candidates': [], 'tier': None, 'method': None}
            if not item['key']:
                item['reason'] = 'empty name'
                out[nm] = item
                continue
            for tier, use_county, method, conf in (
                    (3, cty, 'name_state_county', CONF_NAME_STATE_COUNTY),
                    (4, None, 'name_state', CONF_NAME_STATE)):
                if tier == 3 and not (st and use_county):
                    continue
                clusters, per = self._name_tier([nm], st, use_county)
                if not clusters:
                    continue
                cands = self._name_candidates(clusters, per, method, conf, st, use_county)
                item.update(candidates=cands, tier=tier, method=method)
                if len(clusters) == 1:
                    item['status'] = 'resolved'
                else:
                    item['status'] = 'ambiguous'
                    item['reason'] = '%d physical mines named %r in %s' % (
                        len(clusters), nm, st or 'any state')
                break
            if item['status'] == 'unmatched':
                item['near'] = self._fuzzy([nm], st)
                item['reason'] = ('no located site named %r in %s%s' % (
                    nm, st or 'any state',
                    '; near-misses reported, never resolved' if item['near'] else ''))
            out[nm] = item
        return out

    def _place_document(self, doc, names, place_rows):
        """Why this document is a district/county file, or ``None``.

        The id map is the authority when it has spoken (a ``district`` or
        ``county`` relation row says the corpus filed this under a place);
        otherwise only a document that *says* so — a place-typed doc_type or
        doc_class, or a title that reads a mine_names entry as '<name>
        district' — counts.  Nothing is inferred from the names alone."""
        if place_rows:
            return 'id map relation %s for %s' % (
                '/'.join(sorted({r.get('relation') for r in place_rows})),
                ', '.join(sorted({str(r.get('front_end_id')) for r in place_rows})))
        blob = ' '.join(str(doc.get(k) or '') for k in ('doc_type', 'doc_class')).strip()
        if re.search(r'\b(district|county)\b', blob, re.I):
            return 'doc_type/doc_class %r names a place' % blob
        title = str(doc.get('title') or '')
        for nm in names:
            core = core_label(nm)
            if core and re.search(r'\b' + re.escape(core) + r'\s+(?:mining\s+)?district\b',
                                  title, re.I):
                return 'title reads %r as a district' % nm
        return None

    def resolve(self, doc):
        """A ``ws13_documents``-shaped row -> the one located site it describes, or a park.

        ``doc`` carries ``sha256, state, county, mine_ids, mine_names, title,
        portal, doc_type`` and ``front_end_ids`` — the ``ws13_mine_id_map``
        rows the driver joined (``{'front_end_id','method','relation',
        'confidence','verified'}``).  Returns ``{'status': 'resolved'|'parked',
        'candidates', 'reason', 'tiers_tried', 'per_item', ...}``; every
        candidate says exactly what matched.
        """
        st = _state_code(doc.get('state'))
        county = _county_key(doc.get('county')) or None
        names = [str(n).strip() for n in (doc.get('mine_names') or ()) if str(n or '').strip()]
        ids = [str(i).strip() for i in (doc.get('mine_ids') or ()) if str(i or '').strip()]
        rows = [r for r in (doc.get('front_end_ids') or ()) if r]
        tiers, per_item = [], []
        base = {'sha256': doc.get('sha256'), 'state': st, 'county': county,
                'tiers_tried': tiers, 'per_item': per_item}

        def done(status, reason, cands, **extra):
            out = dict(base, status=status, reason=reason, candidates=cands)
            out.update(extra)
            return out

        def settle(tier, method, clusters, conf, evidence_for, label):
            cands = [self._candidate(c, method, conf, evidence_for(c)) for c in clusters]
            if len(clusters) == 1:
                tiers.append({'tier': tier, 'method': method, 'outcome': 'resolved',
                              'candidates': cands})
                return done('resolved', 'tier %d %s: %s' % (tier, method, label), cands)
            tiers.append({'tier': tier, 'method': method,
                          'outcome': 'ambiguous: %d physical mines' % len(clusters),
                          'candidates': cands})
            return done('parked', 'ambiguous: tier %d %s yields %d physical mines (%s)' % (
                tier, method, len(clusters), label), cands)

        # -- tier 1: a verified identity row of ws13_mine_id_map
        identity = [r for r in rows if (r.get('relation') or 'identity') == 'identity']
        found, notes = [], []
        for r in identity:
            fid = str(r.get('front_end_id') or '')
            if not r.get('verified'):
                notes.append('%s: identity row not verified (method %s, confidence %s): '
                             'reported, not used' % (fid, r.get('method'), r.get('confidence')))
                continue
            sites, note = self.find_id(fid, st, scan=True)
            if not sites:
                notes.append('%s: %s' % (fid, note))
                continue
            for s in sites:
                if not s['located']:
                    notes.append('%s: %s has no coordinate (unlocated)' % (fid, s['mine_key']))
                    continue
                found.append((s, r, note))
        if found:
            by_key = {}
            for s, r, note in found:
                by_key.setdefault(s['mine_key'], (s, r, note))
            clusters = self._extend(self._cluster([v[0] for v in by_key.values()],
                                                  same_name=False))

            def ev1(c):
                keys = {s['mine_key'] for s in c}
                hits = [v for v in by_key.values() if v[0]['mine_key'] in keys]
                return {'rows': [{'front_end_id': r.get('front_end_id'), 'method': r.get('method'),
                                  'confidence': r.get('confidence'), 'matched': s['mine_key'],
                                  'name': s['name'], 'note': note} for s, r, note in hits],
                        'state': st, 'county': county,
                        'matched_summary': '; '.join('verified identity row %s -> %s (%s)' % (
                            r.get('front_end_id'), s['mine_key'], s['name']) for s, r, _ in hits),
                        'notes': notes}
            return settle(1, 'identity_row', clusters, CONF_IDENTITY_ROW, ev1,
                          'verified identity row%s' % ('s' if len(found) > 1 else ''))
        tiers.append({'tier': 1, 'method': 'identity_row',
                      'outcome': ('no identity row joined' if not identity
                                  else 'no located verified identity row'),
                      'notes': notes})

        # -- tier 2: an embedded survey code in mine_ids that is itself a site id
        found, notes = {}, []
        for code in ids:
            m = _NS_ID.match(code)
            if m:
                sites, note = self.find_id(code, st, scan=False)
            elif code.isdigit():
                notes.append('%s: digit-only, never probed (MRDS dep_ids and NBMG file '
                             'numbers collide)' % code)
                continue
            elif _looks_like_code(code):
                sites = self._by_code(code, st)
                note = 'code %s in %s' % (code.upper(), st or 'every state')
            else:
                notes.append('%s: not code-shaped (a name or a place; names are tiers 3-4)' % code)
                continue
            if not sites:
                notes.append('%s: no site carries this code (%s)' % (code, note))
                continue
            for s in sites:
                if not s['located']:
                    notes.append('%s: %s has no coordinate (unlocated)' % (code, s['mine_key']))
                    continue
                found.setdefault(s['mine_key'], (s, code, note))
                per_item.append({'item': code, 'kind': 'code', 'mine_key': s['mine_key']})
        if found:
            clusters = self._extend(self._cluster([v[0] for v in found.values()]))

            def ev2(c):
                keys = {s['mine_key'] for s in c}
                hits = [v for v in found.values() if v[0]['mine_key'] in keys]
                return {'codes': [{'code': code, 'matched': s['mine_key'], 'site_id': s['id'],
                                   'name': s['name'], 'note': note} for s, code, note in hits],
                        'state': st, 'county': county,
                        'matched_summary': '; '.join('code %s = site id %r -> %s (%s)' % (
                            code, s['id'], s['mine_key'], s['name']) for s, code, note in hits),
                        'notes': notes}
            return settle(2, 'embedded_code', clusters, CONF_EMBEDDED_CODE, ev2,
                          'embedded code%s %s' % ('s' if len(found) > 1 else '',
                                                  ', '.join(v[1] for v in found.values())))
        tiers.append({'tier': 2, 'method': 'embedded_code',
                      'outcome': 'no mine_ids' if not ids else 'no code names a located site',
                      'notes': notes})

        # -- tiers 3/4: names, unless the document is a place file
        place_rows = [r for r in rows if (r.get('relation') or 'identity') in ('district', 'county')]
        place = self._place_document(doc, names, place_rows)
        if names and not place:
            for tier, use_county, method, conf in (
                    (3, county, 'name_state_county', CONF_NAME_STATE_COUNTY),
                    (4, None, 'name_state', CONF_NAME_STATE)):
                if tier == 3 and not (st and county):
                    tiers.append({'tier': 3, 'method': method,
                                  'outcome': 'skipped: no %s on the document' % (
                                      'state' if not st else 'county')})
                    continue
                clusters, per = self._name_tier(names, st, use_county)
                for nm, info in per.items():
                    per_item.append({'item': nm, 'kind': 'name', 'tier': tier,
                                     'matched': info['matched'],
                                     'county_unrecorded': info['county_unrecorded'],
                                     'unlocated': info['unlocated']})
                if clusters:
                    cands = self._name_candidates(clusters, per, method, conf, st, use_county)
                    if len(clusters) == 1:
                        tiers.append({'tier': tier, 'method': method, 'outcome': 'resolved',
                                      'candidates': cands, 'per_name': per})
                        return done('resolved', 'tier %d %s: %s' % (
                            tier, method, cands[0]['evidence']['summary']), cands)
                    tiers.append({'tier': tier, 'method': method,
                                  'outcome': 'ambiguous: %d physical mines' % len(clusters),
                                  'candidates': cands, 'per_name': per})
                    named = {nm for nm in per if per[nm]['matched']}
                    if len(named) > 1:
                        reason = 'several mines named: %d physical mines across %s' % (
                            len(clusters), ', '.join(sorted(named)))
                    else:
                        reason = 'ambiguous: tier %d %s yields %d physical mines for %r' % (
                            tier, method, len(clusters), sorted(named)[0] if named else names)
                    return done('parked', reason, cands, per_name=per)
                unl = sorted({k for i in per.values() for k in i['unlocated']})
                tiers.append({'tier': tier, 'method': method,
                              'outcome': ('only unlocated sites carry this name: %s' % ', '.join(unl)
                                          if unl else 'no located site matches'),
                              'per_name': per})
        elif names:
            for tier in (3, 4):
                tiers.append({'tier': tier, 'outcome': 'skipped: mine_names are place names (%s)' % place})
        else:
            for tier in (3, 4):
                tiers.append({'tier': tier, 'outcome': 'no mine_names'})

        # -- tier 5: a district/county container is not a mine
        if place:
            tiers.append({'tier': 5, 'method': 'container',
                          'outcome': 'parked: district/county container'})
            return done('parked', 'district/county container: %s' % place, [],
                        container={'reason': place, 'rows': place_rows, 'names': names,
                                   'hint': 'resolve_names(_named_mines(text, set()), state, county) '
                                           'lists the mines the text names; targets_for carves them'})

        # -- nothing exact: report the near-misses and the id map's own guesses, then park
        near = self._fuzzy(names, st) if names else []
        if near:
            tiers.append({'tier': 'fuzzy', 'method': 'fuzzy',
                          'outcome': 'ambiguous: near-misses reported, never resolved',
                          'near': near})
        guessed = [r for r in rows if r.get('method') == 'fuzzy_name']
        if guessed:
            tiers.append({'tier': 'id_map_fuzzy', 'method': 'fuzzy_name',
                          'outcome': 'id map fuzzy_name rows are guesses: reported, never resolved',
                          'rows': guessed})
        if not (identity or ids or names):
            return done('parked', 'nothing to resolve: the document names no mine', [])
        return done('parked', 'no located site matches%s' % (
            '; %d near-misses reported, never resolved' % len(near) if near else ''), [])


def targets_for(doc, text, index=None):
    """What to carve for one document: ``(mine_key, cores)`` per target.

    The resolved mine, plus — for a district/county container or a document
    that names several mines — every mine the text names that resolves to
    exactly one physical mine (``SiteIndex.resolve_names`` over
    ``_named_mines``).  Each target carries the ``other_cores`` that
    ``sections`` needs so one mine's adit never lands on another: every other
    mine name the text or the document mentions.

    ``text`` is a string or the document's page list.  Returns
    ``{'resolution', 'targets': [{'mine_key', 'cores', 'other_cores',
    'candidate', 'via'}], 'other_cores', 'named'}``.
    """
    index = index or SiteIndex.load()
    pages = list(text) if isinstance(text, (list, tuple)) else [text or '']
    full = '\n\n'.join(p or '' for p in pages)
    res = index.resolve(doc)
    st, county = res['state'], res['county']
    named_cores = _named_mines(full, set())
    doc_cores = {core_label(n) for n in (doc.get('mine_names') or ()) if core_label(n)}

    targets = []

    def cores_of(cand, extra=()):
        out = set()
        for a in [cand['name']] + list(cand.get('aliases') or ()) + \
                [m['name'] for m in cand.get('also') or ()] + list(extra):
            c = core_label(a)
            if c and len(c) >= 3:
                out.add(c)
        return out

    def add(cand, cores, via):
        for t in targets:
            if t['mine_key'] == cand['mine_key']:
                t['cores'] |= cores
                return
        targets.append({'mine_key': cand['mine_key'], 'cores': set(cores),
                        'candidate': cand, 'via': via})

    if res['status'] == 'resolved':
        cand = res['candidates'][0]
        add(cand, cores_of(cand, cand['evidence'].get('names') or ()), 'resolved')

    named = {}
    reason = res.get('reason') or ''
    if res['status'] == 'parked' and (reason.startswith('district/county container')
                                      or reason.startswith('several mines named')):
        named = index.resolve_names(sorted(named_cores | doc_cores), st, county)
        for nm in sorted(named):
            item = named[nm]
            if item['status'] == 'resolved':
                add(item['candidates'][0], cores_of(item['candidates'][0], [nm]), 'named:' + nm)

    all_cores = set(named_cores) | doc_cores
    for t in targets:
        all_cores |= t['cores']
    out = []
    for t in targets:
        own = {c.lower() for c in t['cores']}
        out.append({'mine_key': t['mine_key'], 'cores': sorted(t['cores']),
                    'other_cores': sorted(c for c in all_cores if c.lower() not in own),
                    'candidate': t['candidate'], 'via': t['via']})
    return {'resolution': res, 'targets': out, 'other_cores': sorted(all_cores),
            'named': named}
