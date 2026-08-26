"""geomodel.resolve — turn a mine *name* into a located, cited mine.

The index is built over ``site/data/grades/grades.json``, the same 3,369-mine
columnar bundle the site's grade layer uses, so a resolved mine carries the
citation the site already shows: the quoted sentence, the publication and its
URL.

The one rule here: **candidates are returned, never picked.**  Historic mining
names collide constantly — there are Bluebirds in four states — and silently
choosing one would georeference the wrong hole in the ground.  ``lookup``
returns a ranked list and the caller (an agent, or an operator) chooses.

Not every row in the bundle is located: a mine whose ``x``/``y`` are null is
returned with ``located: False`` so the caller can see that a coordinate must
come from somewhere else before a model can be built.

Collar elevation is sampled from the same cached AWS terrarium tiles as the
map's 3-D mode; ``elevation`` returns ``None`` rather than 0 when the tiles are
unreachable, because a zero elevation is a lie about a mountain.
"""
import difflib
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PIPELINES = os.path.normpath(os.path.join(HERE, '..'))
ROOT = os.path.normpath(os.path.join(PIPELINES, '..'))
GRADES = os.path.join(ROOT, 'site', 'data', 'grades', 'grades.json')
if PIPELINES not in sys.path:
    sys.path.insert(0, PIPELINES)

INDEX_VERSION = 'nwmm-resolve/1'

#: grade columns carried through verbatim; units are per column, as stated in
#: the bundle's own ``note`` (echoed into every result).
GRADE_COLUMNS = ('au', 'ag', 'pb', 'zn', 'cu', 'sb', 'wo3', 'usd')

#: words that describe *what kind of thing* a property is, not which one it is
SUFFIXES = ('mine', 'mines', 'mining', 'company', 'co', 'group', 'claim', 'claims',
            'property', 'prospect', 'prospects', 'workings', 'lode', 'district',
            'the', 'and', 'of')

_PARENS = re.compile(r'\([^)]*\)')
_NONWORD = re.compile(r'[^a-z0-9 ]+')
_NUMBERED = re.compile(r'\bno\.?\s*(\d+)\b')


def normalise(name):
    """'Lucky Girl group (Montana Gold Mining Co.)' -> 'lucky girl'."""
    s = (name or '').lower()
    s = _PARENS.sub(' ', s)
    s = _NUMBERED.sub(r' no\1 ', s)
    s = _NONWORD.sub(' ', s)
    toks = [t for t in s.split() if t and t not in SUFFIXES]
    return ' '.join(toks)


class Index(object):
    """Name index over the grades bundle.  Build once, query many times."""

    def __init__(self, bundle, path=GRADES):
        self.bundle = bundle
        self.path = path
        self.n = int(bundle.get('n') or len(bundle.get('name') or []))
        self.note = bundle.get('note', '')
        self.generated = bundle.get('generated', '')
        self._keys = [normalise(bundle['name'][i]) for i in range(self.n)]
        self._exact = {}
        for i, k in enumerate(self._keys):
            self._exact.setdefault(k, []).append(i)

    # ------------------------------------------------------------- accessors
    def _col(self, key, i):
        col = self.bundle.get(key)
        return col[i] if isinstance(col, list) and i < len(col) else None

    def row(self, i):
        """One candidate, with its citation."""
        lon, lat = self._col('x', i), self._col('y', i)
        grades = dict((c, self._col(c, i)) for c in GRADE_COLUMNS
                      if self._col(c, i) is not None)
        return {
            'mine_id': 'grades:%d' % i,
            'name': self._col('name', i),
            'state': self._col('st', i),
            'district': self._col('dist', i),
            'county': self._col('cnty', i),
            'commodity': self._col('com', i),
            'lon': lon,
            'lat': lat,
            'located': lon is not None and lat is not None,
            'grades': grades,
            'basis': self._col('basis', i),
            'years': self._col('yrs', i),
            'tonnage': self._col('ton', i),
            'deposit': self._col('dep', i),
            'quote': self._col('quote', i),
            'source': self._col('src', i),
            'source_url': self._col('url', i),
        }

    def get(self, mine_id):
        """Look a mine up by the id ``lookup`` handed out."""
        i = parse_mine_id(mine_id)
        if not 0 <= i < self.n:
            raise KeyError(mine_id)
        return self.row(i)

    # ---------------------------------------------------------------- search
    def lookup(self, name, state=None, district=None, county=None, limit=8):
        """Ranked candidates.  Never fewer than every exact match, never a pick."""
        key = normalise(name)
        if not key:
            return []
        scored = {}
        for i in self._exact.get(key, ()):
            scored[i] = (1.0, 'exact')
        want = set(key.split())
        for i, k in enumerate(self._keys):
            if i in scored or not k:
                continue
            toks = set(k.split())
            if want <= toks or toks <= want:
                score, how = 0.9 if want == toks else 0.8, 'tokens'
            else:
                score = difflib.SequenceMatcher(None, key, k).ratio()
                how = 'fuzzy'
                if score < 0.82:
                    continue
            scored[i] = (round(score, 6), how)

        out = []
        for i, (score, how) in scored.items():
            row = self.row(i)
            narrowed = _narrow(row, state, district, county)
            if narrowed is None:
                continue
            row['score'] = round(score + narrowed, 6)
            row['match'] = how
            out.append(row)
        out.sort(key=lambda r: (-r['score'], parse_mine_id(r['mine_id'])))
        return out[:limit]


def _narrow(row, state, district, county):
    """Bonus for matching the caller's geography; ``None`` rejects the row.
    A stated state that disagrees is a rejection — an agent that says Nevada
    does not want an Idaho mine — while district and county only add weight,
    because the bundle's district names are not exhaustive."""
    bonus = 0.0
    if state:
        if not row['state']:
            return None
        if row['state'].strip().upper() != state.strip().upper():
            return None
        bonus += 0.05
    for want, got in ((district, row['district']), (county, row['county'])):
        if want and got and normalise(want) == normalise(got):
            bonus += 0.03
    return bonus


def parse_mine_id(mine_id):
    m = re.match(r'^grades:(\d+)$', str(mine_id or ''))
    if not m:
        raise ValueError('mine_id must look like "grades:1841", got %r' % (mine_id,))
    return int(m.group(1))


# ------------------------------------------------------------------ loading
_CACHE = {}


def load_index(path=GRADES):
    """Load (and memoise) the index.  Raises if the bundle is missing."""
    path = os.path.abspath(path)
    if path not in _CACHE:
        with open(path, encoding='utf-8') as fh:
            _CACHE[path] = Index(json.load(fh), path)
    return _CACHE[path]


def lookup(name, state=None, district=None, county=None, limit=8, index=None):
    """``{'query', 'candidates', 'ambiguous', 'note'}`` — the tool-shaped result."""
    idx = index or load_index()
    cands = idx.lookup(name, state, district, county, limit)
    # Only a single exact name match is unambiguous.  "Bluebird" pulling in
    # three "Blue Bird"s in three states is exactly the case that must reach a
    # human or an agent rather than being resolved by score.
    unambiguous = len(cands) == 1 and cands[0]['match'] == 'exact'
    return {
        'query': {'name': name, 'state': state, 'district': district, 'county': county},
        'candidates': cands,
        'ambiguous': not unambiguous,
        'located': sum(1 for c in cands if c['located']),
        'note': idx.note,
        'index_version': INDEX_VERSION,
        'bundle_generated': idx.generated,
    }


def which_mine_gap(result):
    """The ``which_mine`` question for an ambiguous lookup, in the same shape
    as ``narrative``'s gaps so the agent answers both the same way."""
    cands = result['candidates']
    if not cands:
        return {'id': 'which_mine', 'field': 'mine_id', 'required': True, 'kind': 'no_match',
                'question': 'No mine in the grades bundle matches %r. Give a different name, '
                            'or supply lon/lat directly.' % result['query']['name'],
                'options': []}
    return {
        'id': 'which_mine', 'field': 'mine_id', 'required': True, 'kind': 'which_mine',
        'question': '%d mines match %r. Which one is meant?' % (len(cands), result['query']['name']),
        'options': [{'value': c['mine_id'],
                     'label': '%s — %s%s%s' % (
                         c['name'], c['state'] or '?',
                         (', %s district' % c['district']) if c['district'] else '',
                         '' if c['located'] else ' (no coordinate on file)')}
                    for c in cands] + [{'value': None, 'label': 'none of these'}],
    }


# ---------------------------------------------------------------- elevation
def elevation(lon, lat, zoom=13, offline=False):
    """Collar elevation (m) from the cached terrarium tiles, or ``None``.

    ``None`` is a real answer: it means the tile was not cached and could not
    be fetched, and the caller must decide what to do rather than build a mine
    at sea level."""
    from leapfrog_export import Terrain

    try:
        return Terrain(zoom, offline=offline).sample(lon, lat)
    except Exception:
        return None


def site(mine_id, zoom=13, offline=False, index=None):
    """A resolved mine plus the collar elevation: everything ``agentbuild``
    needs to place a portal in the world."""
    row = (index or load_index()).get(mine_id)
    if not row['located']:
        row['elevation_m'] = None
        row['elevation_source'] = 'unlocated: the bundle has no coordinate for this mine'
        return row
    z = elevation(row['lon'], row['lat'], zoom=zoom, offline=offline)
    row['elevation_m'] = z
    row['elevation_source'] = ('AWS Terrain Tiles (Mapzen terrarium: 3DEP/SRTM composite), zoom %d' % zoom
                               if z is not None else
                               'terrain tile unavailable — collar elevation unknown')
    row['terrain_zoom'] = zoom
    return row
