"""geomodel.narrative — turn USGS/USBM-style mine prose into a typed
``WorkingsSpec`` plus the questions the prose does not answer.

Deterministic: no model calls, no network, no randomness.  The same text
always yields the same spec, byte for byte.

    >>> spec = parse("An adit driven N45E for 900 feet cuts the vein.")
    >>> spec['elements'][0]['kind'], spec['elements'][0]['length_m']
    ('adit', 274.32)

Three rules govern everything below, because a model built from prose is a
digitising bridge and not new evidence (ASSUMPTIONS #48):

1. **Never invent.**  A missing bearing is a gap, never a default.  No element
   is emitted carrying a fabricated number.  The one exception is a *definitional*
   value — an unqualified "shaft" is vertical by definition of the word — and
   those are listed in ``element['defaults']`` so an auditor can see them.
2. **Every element carries its verbatim quote and character span** into the
   source text, so any number in the model can be traced back to the sentence
   that produced it.
3. **Confidence is per field.**  ``element['fields']`` maps each populated
   field to ``surveyed`` (traced off a georeferenced plan), ``described``
   (parsed from this text) or ``assumed`` (supplied in answer to a gap).  The
   element's own confidence is the weakest of them.

What the grammar covers (every class was taken from the WS12 corpus):

  bearing   N45E · N 45° E · N. 45° E. · S 30 W · 045° · due east · northeasterly
  length    900 ft · 900 feet · 900' · 1,200-foot · 275 m · about 400 feet
  vertical  shaft sunk to 300 ft · inclined shaft 45°, 420 ft · winze 120 ft
            below the 400 level
  level     400 level · No. 3 level · adit level · 100-ft level · main haulage level
  element   adit tunnel crosscut drift incline decline raise winze shaft stope
            glory hole portal trench pit
  relation  driven N45E from the portal · from the 300 level · connects the
            200 and 300 levels
  count     three adits · two shafts · a series of raises

A phrasing the grammar does not know becomes a gap of kind ``unparsed`` with
the sentence quoted — never a silent omission — and ``coverage`` reports the
proportion of mining sentences that produced geometry, so a systematically
missed construction is visible on every parse.
"""
import hashlib
import json
import re

PARSER_VERSION = 'nwmm-narrative/1'

FT = 0.3048

# --------------------------------------------------------------- vocabulary
#: surface word -> canonical workings type (see geomodel.workings.TYPES).
#: "incline" is an *inclined shaft* in historic prose, not a modern ramp;
#: "decline"/"ramp" are the ramp.  Both are recorded, and the dip of an
#: incline is required rather than assumed.
KIND_WORDS = [
    ('glory hole', 'pit'), ('open cut', 'pit'), ('opencut', 'pit'),
    ('inclined shaft', 'shaft'), ('incline shaft', 'shaft'),
    ('haulage tunnel', 'tunnel'), ('haulageway', 'drift'),
    ('crosscut', 'crosscut'), ('cross-cut', 'crosscut'), ('cross cut', 'crosscut'),
    ('adit', 'adit'), ('tunnel', 'tunnel'), ('drift', 'drift'),
    ('decline', 'decline'), ('ramp', 'decline'),
    ('incline', 'shaft'), ('winze', 'winze'), ('raise', 'raise'),
    ('shaft', 'shaft'), ('stope', 'stope'), ('portal', 'portal'),
    ('trench', 'trench'), ('prospect pit', 'pit'), ('pit', 'pit'),
    ('stoped', 'stope'), ('stoping', 'stope'), ('stopes', 'stope'),
    ('level', 'level'),
]

#: kinds whose geometry is vertical-first (a measure is a depth, not a run)
VERTICAL = ('shaft', 'winze')

#: what each kind must have before it can be built
REQUIRED = {
    'adit': ('bearing_deg', 'length_m'),
    'tunnel': ('bearing_deg', 'length_m'),
    'drift': ('bearing_deg', 'length_m', 'level'),
    'crosscut': ('bearing_deg', 'length_m', 'level'),
    'decline': ('bearing_deg', 'length_m'),
    'shaft': ('depth_m',),
    'winze': ('depth_m', 'level'),
    'raise': ('length_m', 'level'),
    'stope': ('length_m', 'level'),
    'trench': ('length_m',),
    'pit': (),
    'portal': (),
}

#: verbs that mark a sentence as describing workings even when nothing parses
MINING_WORDS = re.compile(
    r'\b(driven|drove|drift(?:ed|s)?|sunk|sink|sinking|stoped|stoping|raised|'
    r'crosscut|extended|opened|developed|breasted|drifting|run\s+(?:in|on)|'
    r'adit|tunnel|shaft|winze|raise|stope|portal|level|incline|decline|workings)\b',
    re.I)

QUADRANT_WORDS = {
    'north': 0.0, 'northerly': 0.0, 'northward': 0.0,
    'northeast': 45.0, 'northeasterly': 45.0, 'northeastward': 45.0,
    'east': 90.0, 'easterly': 90.0, 'eastward': 90.0,
    'southeast': 135.0, 'southeasterly': 135.0, 'southeastward': 135.0,
    'south': 180.0, 'southerly': 180.0, 'southward': 180.0,
    'southwest': 225.0, 'southwesterly': 225.0, 'southwestward': 225.0,
    'west': 270.0, 'westerly': 270.0, 'westward': 270.0,
    'northwest': 315.0, 'northwesterly': 315.0, 'northwestward': 315.0,
}

NUMBER_WORDS = {'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5,
                'six': 6, 'seven': 7, 'eight': 8, 'nine': 9, 'ten': 10}

VAGUE_COUNT = re.compile(r'\b(a\s+series\s+of|several|numerous|many|a\s+number\s+of)\s+', re.I)

# ------------------------------------------------------------------ patterns
_NUM = r'\d[\d,]*(?:\.\d+)?'

RE_KIND = re.compile(r'\b(' + '|'.join(
    re.escape(w).replace(r'\ ', r'[\s-]')
    for w, _ in sorted(KIND_WORDS, key=lambda wk: -len(wk[0]))) + r')(e?s)?\b', re.I)

# N45E · N 45° E · N. 45° 30' E.
RE_BEARING_QUAD = re.compile(
    r'\b(?P<q1>[NS])\.?\s*(?P<deg>' + _NUM + r')\s*(?:°|&deg;|deg\.?|degrees?)?'
    r"(?:\s*(?P<min>\d{1,2})\s*['′])?\s*(?P<q2>[EW])\.?", re.I)

# azimuth 045° · bearing of 45 degrees · course N. 45 E. handled above
RE_BEARING_AZ = re.compile(
    r'\b(?:azimuth|bearing|course|strike)\s*(?:of|:)?\s*(?P<deg>' + _NUM + r')\s*(?:°|deg\.?|degrees?)?', re.I)

# a bare three-digit azimuth is unambiguous ("driven on 045°")
RE_BEARING_BARE = re.compile(r'(?<![\d.])(?P<deg>[0-3]\d\d)\s*(?:°|deg\.?|degrees?)')

RE_BEARING_DUE = re.compile(r'\bdue\s+(?P<word>north|south|east|west|northeast|northwest|southeast|southwest)\b', re.I)

RE_BEARING_WORD = re.compile(r'\b(?P<word>' + '|'.join(
    sorted((w for w in QUADRANT_WORDS if w.endswith(('ly', 'ward'))), key=len, reverse=True)) + r')\b', re.I)

RE_DIP = re.compile(
    r'\b(?:inclined|inclination|dip(?:s|ping|ped)?|pitch(?:es|ing)?|at\s+an\s+angle\s+of|angle\s+of)'
    r'[^.;]{0,24}?(?P<deg>' + _NUM + r')\s*(?:°|deg\.?|degrees?)', re.I)

# "sunk 420 feet at 45 degrees" — the cue and the angle sit far apart, so this
# form is matched on its own rather than by widening RE_DIP's window.
RE_DIP_AT = re.compile(r'\bat\s+(?P<deg>' + _NUM + r')\s*(?:°|deg\.?|degrees?)\b', re.I)

RE_DIP_POST = re.compile(r'(?P<deg>' + _NUM + r')\s*(?:°|deg\.?|degrees?)\s*(?:incline|from\s+the\s+horizontal|dip)', re.I)

RE_LEVEL_NUM = re.compile(
    r'\b(?P<lv>' + _NUM + r')[\s-]*(?:ft\.?|foot|feet|m|metre|meter)?[\s-]*level\b', re.I)
RE_LEVEL_NO = re.compile(r'\bNo\.?\s*(?P<n>\d+)\s+level\b', re.I)
RE_LEVEL_NAMED = re.compile(r'\b(?P<n>adit|main\s+haulage|haulage|tunnel|surface|lower|upper|main)\s+level\b', re.I)

RE_MEASURE = re.compile(
    r'(?P<approx>\b(?:about|approximately|some|roughly|nearly|over|more\s+than)\s+)?'
    r'(?P<num>' + _NUM + r')'
    r"(?:\s*(?:to|-|–)\s*(?P<num2>" + _NUM + r"))?"
    r"[\s-]*(?P<unit>ft\.?|feet|foot|['′]|m\b|meters?|metres?)", re.I)

RE_DEPTH_CUE = re.compile(r'\b(sunk|sinking|sank|depth|deep|below|down)\b', re.I)

#: "at an elevation of 6,450 feet" and "180 feet higher" are both *heights*.
#: Reading either as a drive length is how a 1,140-foot adit becomes a
#: 6,450-foot one, so they are recognised and set aside.
RE_ELEVATION_CUE = re.compile(r'\b(?:elevation|altitude|elev\.?)\s+(?:of\s+)?(?:about\s+)?$', re.I)
RE_OFFSET_CUE = re.compile(r'^\s*(?:higher|lower)\b', re.I)

RE_FROM = re.compile(
    r'\bfrom\s+(?:the\s+)?(?P<what>portal|surface|collar|adit|shaft|tunnel|'
    r'bottom\s+of\s+the\s+shaft|(?P<lv>' + _NUM + r')[\s-]*(?:ft\.?|foot|feet)?[\s-]*level)', re.I)

RE_FROM_TO = re.compile(
    r'\bfrom\s+(?:the\s+)?(?P<a>' + _NUM + r')[\s-]*(?:ft\.?|foot|feet)?[\s-]*level\s+'
    r'(?:up\s+)?to\s+(?:the\s+)?(?P<b>' + _NUM + r')[\s-]*(?:ft\.?|foot|feet)?[\s-]*level', re.I)

#: a level that is itself the subject of a drive verb ("the 300 level was
#: extended 450 feet") describes a drift; every other level mention is a
#: referent that qualifies a neighbouring element.
RE_LEVEL_SUBJECT = re.compile(
    r'level\b\s*(?:(?:was|were|is|are|has|had|have)\s+)?(?:been\s+)?'
    r'(?:driven|drifted|extended|run|advanced|continued|opened)\b', re.I)

RE_CONNECTS = re.compile(
    r'\bconnect(?:s|ing|ed)?\s+(?:the\s+)?(?P<a>' + _NUM + r')\s*(?:and|to|with)\s*(?:the\s+)?(?P<b>' + _NUM + r')'
    r'[\s-]*(?:ft\.?|foot|feet)?[\s-]*levels?\b', re.I)

RE_COUNT = re.compile(r'\b(?P<n>' + '|'.join(NUMBER_WORDS) + r'|\d{1,2})\s+$', re.I)

RE_NAME = re.compile(r'((?:No\.?\s*\d+|[A-Z][A-Za-z\'-]+|\d+)(?:\s+(?:[A-Z][A-Za-z\'-]+|\d+))*)\s*$')

#: "of" only makes a reference when it is definite — "the bottom of the shaft"
#: refers to a shaft, whereas "a series of raises" counts them.
RE_REF_PREFIX = re.compile(
    r'(?:\b(?:from|to|at|on|in|near|below|above|under|beneath|beyond|toward|towards|with|along)\s+'
    r'(?:the\s+|a\s+|an\s+)?|\bof\s+the\s+)'
    # a level is named by a number, so the preposition can sit a number away:
    # "above the 300 level" refers to that level, it does not drive it
    r"(?:\d[\d,]*(?:\.\d+)?[\s-]*(?:ft\.?|foot|feet|m|metre|meter)?[\s-]*)?$", re.I)

RE_REF_SUFFIX = re.compile(r'^[\s-]*level\b', re.I)

#: always a mid-sentence full stop, whatever follows
RE_ABBREV = re.compile(r'\b(?:No|Nos|Mt|Co|Inc|Ft|St|Sec|Figs?|approx|vol|pp)\.$')
#: a lone capital is an initial in "N. 45 E. for 900 ft" but a full stop in
#: "driven N 20 W. The vein was stoped" — what follows decides which.  The
#: same applies to a lowercase unit: "20 oz. silver" runs on, "900 ft. The
#: shaft" does not.
RE_INITIAL = re.compile(r'(?<![A-Za-z])[A-Z]\.$|\b(?:oz|lb|lbs|in|ft|pct|wt|no|cwt)\.$')


# ---------------------------------------------------------------- utilities
def _f(num):
    return float(str(num).replace(',', ''))


def _to_m(value, unit):
    u = unit.lower().rstrip('.')
    if u in ('m', 'meter', 'meters', 'metre', 'metres'):
        return value, 'm'
    return value * FT, 'ft'


def sentences(text):
    """[(start, end, sentence)] with offsets into ``text``; abbreviation-aware."""
    out, start = [], 0
    for m in re.finditer(r'[.;:!?](?=\s)|\n{2,}|$', text):
        end = m.end()
        head = text[start:end]
        if not head.strip():
            start = end
            continue
        if m.group(0) in '.;:!?':
            stem = head.rstrip()
            if RE_ABBREV.search(stem):
                continue                          # "No." — never a break
            nxt = text[end:end + 4].lstrip()
            if RE_INITIAL.search(stem) and nxt[:1] and not nxt[:1].isupper():
                continue                          # "N. 45 E. for ..." — an initial
        out.append((start, end, text[start:end]))
        start = end
        if start >= len(text):
            break
    tail = text[start:]
    if tail.strip():
        out.append((start, len(text), tail))
    return out


class _Mask(object):
    """Character spans already claimed by a higher-priority extractor."""

    def __init__(self):
        self.spans = []

    def claim(self, a, b):
        self.spans.append((a, b))

    def free(self, a, b):
        return not any(a < y and x < b for x, y in self.spans)


def _finditer(pattern, text, mask, off):
    """Matches whose absolute span is not already claimed by a higher-priority
    extractor.  Offsets are absolute into the source text so that anchors in
    one sentence share a single mask and cannot consume the same words twice."""
    for m in pattern.finditer(text):
        if mask.free(off + m.start(), off + m.end()):
            yield m


# ------------------------------------------------------------- field pullers
def _pull_bearing(win, mask, off):
    """-> (bearing_deg, precision, span) or None.  Quadrant form wins."""
    for m in _finditer(RE_BEARING_QUAD, win, mask, off):
        deg = _f(m.group('deg')) + (float(m.group('min')) / 60.0 if m.group('min') else 0.0)
        if deg > 90:
            continue
        q1, q2 = m.group('q1').upper(), m.group('q2').upper()
        az = deg if (q1, q2) == ('N', 'E') else \
            360.0 - deg if (q1, q2) == ('N', 'W') else \
            180.0 - deg if (q1, q2) == ('S', 'E') else 180.0 + deg
        mask.claim(off + m.start(), off + m.end())
        return round(az % 360.0, 4), 'stated', (off + m.start(), off + m.end())
    for m in _finditer(RE_BEARING_DUE, win, mask, off):
        mask.claim(off + m.start(), off + m.end())
        return QUADRANT_WORDS[m.group('word').lower()], 'stated', (off + m.start(), off + m.end())
    for m in _finditer(RE_BEARING_AZ, win, mask, off):
        deg = _f(m.group('deg'))
        if deg > 360:
            continue
        mask.claim(off + m.start(), off + m.end())
        return round(deg % 360.0, 4), 'stated', (off + m.start(), off + m.end())
    for m in _finditer(RE_BEARING_BARE, win, mask, off):
        mask.claim(off + m.start(), off + m.end())
        return round(_f(m.group('deg')) % 360.0, 4), 'stated', (off + m.start(), off + m.end())
    for m in _finditer(RE_BEARING_WORD, win, mask, off):
        mask.claim(off + m.start(), off + m.end())
        return QUADRANT_WORDS[m.group('word').lower()], 'approximate', (off + m.start(), off + m.end())
    return None


def _pull_dip(win, mask, off):
    for pat in (RE_DIP, RE_DIP_POST, RE_DIP_AT):
        for m in _finditer(pat, win, mask, off):
            deg = _f(m.group('deg'))
            if not 0 < deg <= 90:
                continue
            mask.claim(off + m.start(), off + m.end())
            return deg, (off + m.start(), off + m.end())
    return None


def _pull_level(win, mask, off):
    """-> (label, depth_m or None, span) or None.

    Historic levels are named for their depth below the shaft collar, so a
    "300 level" carries a depth; "No. 3 level" and "adit level" do not, and
    become a gap when an elevation is actually needed."""
    for m in _finditer(RE_LEVEL_NO, win, mask, off):
        mask.claim(off + m.start(), off + m.end())
        return 'No. %s' % m.group('n'), None, (off + m.start(), off + m.end())
    for m in _finditer(RE_LEVEL_NUM, win, mask, off):
        val = _f(m.group('lv'))
        metric = re.search(r'\b(m|metre|meter)\b', m.group(0), re.I)
        mask.claim(off + m.start(), off + m.end())
        return (m.group('lv').replace(',', ''), val if metric else val * FT,
                (off + m.start(), off + m.end()))
    for m in _finditer(RE_LEVEL_NAMED, win, mask, off):
        mask.claim(off + m.start(), off + m.end())
        return re.sub(r'\s+', ' ', m.group('n').lower()), None, (off + m.start(), off + m.end())
    return None


def _pull_measures(win, mask, off):
    """``(runs, heights)`` — length-like quantities left unclaimed in the
    window, split into things that are distances travelled and things that are
    elevations."""
    out, heights = [], []
    for m in _finditer(RE_MEASURE, win, mask, off):
        val, unit = _to_m(_f(m.group('num')), m.group('unit'))
        hi = _to_m(_f(m.group('num2')), m.group('unit'))[0] if m.group('num2') else None
        before, after = win[max(0, m.start() - 30):m.start()], win[m.end():m.end() + 10]
        mask.claim(off + m.start(), off + m.end())
        if RE_ELEVATION_CUE.search(before) or RE_OFFSET_CUE.match(after):
            heights.append({'m': val, 'units_in': unit,
                            'span': (off + m.start(), off + m.end()),
                            'kind': 'elevation' if RE_ELEVATION_CUE.search(before) else 'offset'})
            continue                              # claimed, so it cannot be re-read as a run
        out.append({'m': val, 'high_m': hi, 'units_in': unit,
                    'span': (off + m.start(), off + m.end()),
                    'approx': bool(m.group('approx')),
                    'depth_cue': bool(RE_DEPTH_CUE.search(before))})
    return out, heights


def _pull_connects(win, mask, off):
    """-> (lower_level, upper_level) for a raise/winze that joins two levels."""
    for pat in (RE_FROM_TO, RE_CONNECTS):
        for m in _finditer(pat, win, mask, off):
            mask.claim(off + m.start(), off + m.end())
            return (m.group('a').replace(',', ''), m.group('b').replace(',', ''))
    return None


def _pull_from(win, mask, off):
    for m in _finditer(RE_FROM, win, mask, off):
        what = re.sub(r'\s+', ' ', m.group('what').lower())
        mask.claim(off + m.start(), off + m.end())
        if m.group('lv'):
            return {'ref': 'level', 'level': m.group('lv').replace(',', '')}, (off + m.start(), off + m.end())
        if what.startswith('bottom'):
            return {'ref': 'shaft_bottom'}, (off + m.start(), off + m.end())
        return ({'ref': {'collar': 'surface', 'portal': 'portal'}.get(what, what)},
                (off + m.start(), off + m.end()))
    return None


# ---------------------------------------------------------------- the parser
def _anchors(text):
    """Element mentions in text order.  A longer phrase beats a word inside it,
    so "inclined shaft" is one anchor and not two."""
    lookup = dict((re.sub(r'[\s-]+', ' ', w), k) for w, k in KIND_WORDS)
    out = []
    for m in RE_KIND.finditer(text):
        word = re.sub(r'[\s-]+', ' ', m.group(1).lower())
        out.append({'start': m.start(), 'end': m.end(), 'kind': lookup[word],
                    'plural': bool(m.group(2)), 'surface': m.group(0)})
    keep, taken = [], []
    for a in sorted(out, key=lambda a: (-(a['end'] - a['start']), a['start'])):
        if any(a['start'] < y and x < a['end'] for x, y in taken):
            continue
        taken.append((a['start'], a['end']))
        keep.append(a)
    return sorted(keep, key=lambda a: a['start'])


def _windows(anchors):
    """Attribute window per anchor: from the previous anchor's end (or the
    sentence start) to the next anchor's start (or the sentence end)."""
    wins = []
    for i, a in enumerate(anchors):
        lo = anchors[i - 1]['end'] if i and anchors[i - 1]['sent'] == a['sent'] else a['sent'][0]
        hi = anchors[i + 1]['start'] if i + 1 < len(anchors) and anchors[i + 1]['sent'] == a['sent'] \
            else a['sent'][1]
        wins.append((lo, hi))
    return wins


def _is_element(text, a):
    """An anchor is a working of its own unless it is being *referred to*:
    "from the portal", "on the adit level", or any bare level mention that is
    not itself the thing that was driven."""
    if RE_REF_PREFIX.search(text[a['sent'][0]:a['start']]):
        return False
    if RE_REF_SUFFIX.match(text[a['end']:a['end'] + 12]):
        return False
    if a['kind'] == 'level':
        return bool(RE_LEVEL_SUBJECT.match(text[a['start']:a['sent'][1]]))
    return True


def parse(text, mine_id=None):
    """Prose -> ``{'spec_id', 'elements', 'gaps', 'coverage', ...}``."""
    text = text or ''
    sents = sentences(text)
    anchors = _anchors(text)
    for a in anchors:
        a['sent'] = next(((s, e) for s, e, _ in sents if s <= a['start'] < e), (0, len(text)))
    for a in anchors:
        a['is_element'] = _is_element(text, a)

    # Windows are bounded by neighbouring *elements* only.  A bare level
    # reference must not truncate the window of the element it qualifies:
    # "a raise ... from the 500 level to the 400 level, a distance of 100 feet"
    # is one element whose length sits beyond two level mentions.
    elems = [a for a in anchors if a['is_element']]
    wins = _windows(elems)

    masks, built = {}, []
    for a, (lo, hi) in zip(elems, wins):
        mask = masks.setdefault(a['sent'], _Mask())
        el = _element(text, a, lo, hi, len(built) + 1, mask)
        if el is not None:
            built.append(el)

    referents = []
    for a in anchors:
        if a['is_element']:
            continue
        mask = masks.setdefault(a['sent'], _Mask())
        lo = max(a['sent'][0], a['start'] - 32)
        got = _pull_level(text[lo:a['end']], mask, lo)
        if got:
            referents.append((a, {'_referent': got[0], '_depth': got[1]}))
    _attach_referents(built, referents)

    # "The mine is developed by two adits and a vertical shaft" names workings
    # without describing any of them.  Such a mention cannot be built, and
    # promoting it to an element would only pile up required questions that the
    # sentences after it already answer.  It is kept, counted and quoted — just
    # not as geometry.
    elements = [el for el in built if not _is_mention(el)]
    mentions = [el for el in built if _is_mention(el)]
    for i, el in enumerate(elements, 1):
        el['id'] = 'e%d' % i
    for i, el in enumerate(mentions, 1):
        el['id'] = 'm%d' % i
    for el in built:
        el.pop('_anchor', None)                   # parse-time bookkeeping only

    levels = dict((e['level'], e['level_depth_m']) for e in elements
                  if e.get('level') and e.get('level_depth_m') is not None)
    # "connects the 400 and 300 levels" names two levels that no other pattern
    # sees, because only the second one is followed by the word "level".
    for e in elements:
        for label in (e.get('connects') or ()):
            try:
                depth = _f(label)
            except (TypeError, ValueError):
                continue
            levels.setdefault(label, depth if e.get('units_in') == 'm' else depth * FT)
    for m in RE_LEVEL_NUM.finditer(text):
        lv = m.group('lv').replace(',', '')
        val = _f(lv)
        levels.setdefault(lv, val if re.search(r'\b(m|metre|meter)\b', m.group(0), re.I) else val * FT)

    gaps = []
    for el in elements:
        gaps.extend(_gaps_for(el, elements, levels))
    for m in mentions:
        gaps.append(_mention_gap(m))
    gaps.extend(_unparsed_gaps(sents, built))
    for i, g in enumerate(gaps, 1):
        g['id'] = 'g%d' % i

    spec = {
        'schema': 'nwmm-workings-spec/1',
        'parser_version': PARSER_VERSION,
        'mine_id': mine_id,
        'text_sha256': hashlib.sha256(text.encode('utf-8')).hexdigest(),
        'levels': levels,
        'elements': elements,
        'mentions': mentions,
        'gaps': gaps,
    }
    spec['coverage'] = _coverage(text, sents, elements, gaps, mentions)
    spec['spec_id'] = 's' + spec_hash(spec)
    return spec


def spec_hash(spec):
    """Stable identity over the parse: text + mine + parser version."""
    payload = json.dumps({'v': spec['parser_version'], 't': spec['text_sha256'],
                          'm': spec.get('mine_id')}, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()[:8]


#: the fields that make a mention into a working worth placing
MEASURED = ('bearing_deg', 'length_m', 'depth_m', 'height_m', 'level', 'from', 'connects')


def _is_mention(el):
    """True when nothing about this working was actually stated."""
    if el.get('_range'):
        return False
    return not any(el.get(f) is not None for f in MEASURED)


def _mention_gap(m):
    n = m.get('count')
    how_many = ('%d %ss are' % (n, m['kind'])) if n and n > 1 else \
        ('An unstated number of %ss is' % m['kind']) if n is None else ('One %s is' % m['kind'])
    return {'element': m['id'], 'field': None, 'required': False, 'kind': 'mention',
            'question': '%s named here without any dimensions. If these are the workings '
                        'described elsewhere in the text, nothing more is needed; otherwise '
                        'give a bearing and a length.' % how_many,
            'quote': m['quote'], 'span': m['span'],
            'options': [{'value': None, 'label': 'already described elsewhere — nothing to add'}]}


def _attach_referents(elements, referents):
    """A bare "on the 300 level" is not an element; it qualifies the nearest
    element in its sentence that has no level yet — the one before it first,
    since "a drift ... on the 300 level" is the commoner word order."""
    for a, ref in referents:
        pool = [e for e in elements if tuple(e['span']) == tuple(a['sent'])
                and not e.get('level') and e['kind'] in REQUIRED and 'level' in REQUIRED[e['kind']]]
        if not pool:
            pool = [e for e in elements if tuple(e['span']) == tuple(a['sent']) and not e.get('level')]
        if not pool:
            continue
        before = [e for e in pool if e['_anchor'] < a['start']]
        el = before[-1] if before else pool[0]
        el['level'], el['level_depth_m'] = ref['_referent'], ref['_depth']
        el['fields']['level'] = 'described'
        el['confidence'] = _confidence(el)


def _element(text, a, lo, hi, n, mask):
    """One anchor -> an element, a level referent, or nothing."""
    kind, win, off = a['kind'], text[lo:hi], lo

    bearing = _pull_bearing(win, mask, off)
    conn = _pull_connects(win, mask, off)
    frm = _pull_from(win, mask, off)
    level = _pull_level(win, mask, off)
    dip = _pull_dip(win, mask, off)
    measures, heights = _pull_measures(win, mask, off)

    if kind == 'level':
        kind = 'drift'                            # the level itself was driven

    el = {'id': 'e%d' % n, 'kind': kind, 'name': _name(text, lo, a['start']),
          'count': 1, 'units_in': 'ft', 'defaults': [], 'fields': {},
          'quote': _quote(text, a['sent']), 'span': list(a['sent']), '_anchor': a['start']}

    if bearing:
        el['bearing_deg'], el['bearing_precision'] = bearing[0], bearing[1]
        el['fields']['bearing_deg'] = 'described'
    if dip:
        el['dip_deg'] = dip[0]
        el['fields']['dip_deg'] = 'described'
    if level:
        el['level'], el['level_depth_m'] = level[0], level[1]
        el['fields']['level'] = 'described'
    if frm:
        el['from'] = frm[0]
        el['fields']['from'] = 'described'
    if conn:
        el['connects'] = list(conn)
        el['fields']['connects'] = 'described'

    if heights:
        el['heights'] = [{'m': round(h['m'], 4), 'kind': h['kind']} for h in heights]

    primary = measures[0] if measures else None
    if primary is not None:
        el['units_in'] = primary['units_in']
        field = 'depth_m' if (kind in VERTICAL or (primary['depth_cue'] and kind != 'raise')) else 'length_m'
        if primary['high_m'] is None:
            el[field] = round(primary['m'], 4)
            el['fields'][field] = 'described'
            if primary['approx']:
                el['measure_precision'] = 'approximate'
        else:
            # "400 to 500 feet" — a range is a question, not a number
            el['_range'] = {'field': field, 'low_m': round(primary['m'], 4),
                            'high_m': round(primary['high_m'], 4)}
        if len(measures) > 1:
            # a stope's second figure is its back height; an incline's is the
            # vertical it gained.  Both are recorded, neither is guessed at.
            el['height_m' if kind == 'stope' else 'secondary_m'] = round(measures[1]['m'], 4)
            el['fields']['height_m' if kind == 'stope' else 'secondary_m'] = 'described'

    if kind in VERTICAL and 'dip_deg' not in el and 'incline' not in a['surface'].lower():
        el['dip_deg'] = 90.0                      # definitional, not invented
        el['defaults'].append('dip_deg')
        el['fields']['dip_deg'] = 'described'

    cnt = RE_COUNT.search(text[lo:a['start']])
    if a['plural'] and VAGUE_COUNT.search(text[lo:a['start']]):
        el['count'] = None                        # "a series of raises"
    elif a['plural'] and cnt:
        raw = cnt.group('n').lower()
        el['count'] = NUMBER_WORDS.get(raw, int(raw) if raw.isdigit() else 1)

    el['confidence'] = _confidence(el)
    return el


def _name(text, lo, at):
    m = RE_NAME.search(text[lo:at].rstrip())
    if not m:
        return ''
    cand = re.sub(r'^(?:The|A|An)\s+', '', m.group(1).strip())
    cand = re.sub(r'^(?:%s|\d{1,2})\b\s*' % '|'.join(NUMBER_WORDS), '', cand, flags=re.I).strip()
    if len(cand) > 48 or cand.lower() in ('the', 'a', 'an') or not re.search(r'[A-Za-z]', cand):
        return ''
    if not re.search(r'[A-Z]', cand):
        return ''
    return cand


def _quote(text, sent):
    return re.sub(r'\s+', ' ', text[sent[0]:sent[1]]).strip()


def _confidence(el):
    order = {'surveyed': 0, 'described': 1, 'assumed': 2}
    vals = list(el['fields'].values())
    return max(vals, key=lambda v: order.get(v, 1)) if vals else 'described'


# ------------------------------------------------------------------- gaps
def _gaps_for(el, elements, levels):
    out = []
    kind = el['kind']

    rng = el.pop('_range', None)
    if rng:
        out.append({'element': el['id'], 'field': rng['field'], 'required': True,
                    'kind': 'range',
                    'question': 'The %s is given as a range (%.4g–%.4g m). Which value should be modelled?'
                                % (kind, rng['low_m'], rng['high_m']),
                    'quote': el['quote'], 'span': el['span'],
                    'options': [{'value': rng['low_m'], 'label': 'the lower figure'},
                                {'value': rng['high_m'], 'label': 'the upper figure'},
                                {'value': round((rng['low_m'] + rng['high_m']) / 2.0, 4),
                                 'label': 'the midpoint'},
                                {'value': None, 'label': 'unknown — omit this element'}]})

    needs = list(REQUIRED.get(kind, ()))
    if kind in VERTICAL and (el.get('dip_deg') or 90.0) < 90.0:
        needs.append('bearing_deg')               # an incline goes somewhere
    if kind in VERTICAL and 'dip_deg' not in el:
        needs.append('dip_deg')

    for field in needs:
        if el.get(field) is not None:
            continue
        if field == 'level' and (el.get('from') or el.get('connects')):
            continue
        if field == 'length_m' and el.get('connects'):
            continue                              # the two levels fix the length
        if field == 'length_m' and rng and rng['field'] == 'length_m':
            continue
        if field == 'depth_m' and rng and rng['field'] == 'depth_m':
            continue
        out.append({'element': el['id'], 'field': field, 'required': True, 'kind': 'missing',
                    'question': _question(el, field),
                    'quote': el['quote'], 'span': el['span'],
                    'options': _options(el, field, elements, levels)})

    if el.get('bearing_precision') == 'approximate':
        out.append({'element': el['id'], 'field': 'bearing_deg', 'required': False,
                    'kind': 'imprecise',
                    'question': 'The %s bearing is given only as a compass sector (%s° assumed). '
                                'Is a more exact bearing available?' % (kind, el['bearing_deg']),
                    'quote': el['quote'], 'span': el['span'],
                    'options': [{'value': el['bearing_deg'], 'label': 'keep the sector midpoint'}]})

    if el.get('count') is None:
        out.append({'element': el['id'], 'field': 'count', 'required': False, 'kind': 'count',
                    'question': 'An unspecified number of %ss is mentioned; one is modelled. How many were there?'
                                % kind,
                    'quote': el['quote'], 'span': el['span'],
                    'options': [{'value': 1, 'label': 'model the one that is described'}]})
    elif el.get('count', 1) > 1:
        out.append({'element': el['id'], 'field': 'count', 'required': False, 'kind': 'count',
                    'question': '%d %ss are mentioned but only one is described. Model just the described one?'
                                % (el['count'], kind),
                    'quote': el['quote'], 'span': el['span'],
                    'options': [{'value': 1, 'label': 'model the one that is described'},
                                {'value': el['count'], 'label': 'model %d identical copies' % el['count']}]})
    return out


def _question(el, field):
    kind, name = el['kind'], (el.get('name') or '').strip()
    who = ('the %s %s' % (name, kind)) if name else ('the %s' % kind)
    return {
        'bearing_deg': 'No bearing is stated for %s. What bearing was it driven on?' % who,
        'length_m': 'No length is stated for %s. How long was it?' % who,
        'depth_m': 'No depth is stated for %s. How deep was it sunk?' % who,
        'level': 'No level is stated for %s, so its elevation is unknown. Which level is it on?' % who,
        'dip_deg': 'No dip is stated for %s. At what angle below horizontal was it driven?' % who,
    }.get(field, 'What is the %s of %s?' % (field, who))


def _options(el, field, elements, levels):
    opts = []
    if field == 'bearing_deg':
        for other in elements:
            if other['id'] != el['id'] and other.get('bearing_deg') is not None:
                opts.append({'value': other['bearing_deg'],
                             'label': 'same as the %s (%.4g°)' % (other['kind'], other['bearing_deg'])})
                break
    elif field == 'level':
        for lv, depth in sorted(levels.items(), key=lambda kv: kv[0]):
            opts.append({'value': lv, 'label': 'the %s level (%.4g m below collar)' % (lv, depth)})
        opts.append({'value': 'adit', 'label': 'the adit level'})
    opts.append({'value': None, 'label': 'unknown — omit this element'})
    return opts


def _unparsed_gaps(sents, elements):
    """Sentences that talk about workings but produced no geometry."""
    got = set(tuple(e['span']) for e in elements)
    out = []
    for s, e, body in sents:
        if (s, e) in got or not MINING_WORDS.search(body):
            continue
        out.append({'element': None, 'field': None, 'required': False, 'kind': 'unparsed',
                    'question': 'This sentence describes workings in a phrasing the parser does not know. '
                                'Should it contribute geometry, and if so what?',
                    'quote': re.sub(r'\s+', ' ', body).strip(), 'span': [s, e],
                    'options': [{'value': None, 'label': 'nothing to model here'}]})
    return out


def _coverage(text, sents, elements, gaps, mentions=()):
    mining = [s for s in sents if MINING_WORDS.search(s[2])]
    covered = set(tuple(e['span']) for e in elements)
    parsed_chars = sum(e - s for s, e, _ in sents if (s, e) in covered)
    return {
        'chars': len(text),
        'parsed_chars': parsed_chars,
        'sentences': len(sents),
        'mining_sentences': len(mining),
        'sentences_with_elements': len(covered),
        'elements': len(elements),
        'mentions': len(mentions),
        'unresolved': sum(1 for g in gaps if g['required']),
        'questions': len(gaps),
        'unparsed_sentences': sum(1 for g in gaps if g['kind'] == 'unparsed'),
    }


# ------------------------------------------------------------------ answers
def apply_answers(spec, answers):
    """Return a new spec with agent/operator answers folded in.  Answered
    fields are tagged ``assumed`` and the justification is kept verbatim."""
    spec = json.loads(json.dumps(spec))
    by_id = dict((g['id'], g) for g in spec['gaps'])
    els = dict((e['id'], e) for e in spec['elements'])
    already = dict((a['gap'], a) for a in (spec.get('answers') or []))
    applied, dropped = [], set()
    for ans in answers or []:
        g = by_id.get(ans.get('id'))
        if g is None:
            # An agent retrying after a dropped response must not be punished
            # for it: re-sending the same answer is a no-op, and only a
            # *different* answer to a settled question is an error.
            prior = already.get(ans.get('id'))
            if prior is not None:
                if prior.get('value') == ans.get('value'):
                    continue
                raise ValueError('%s was already answered with %r; it cannot be changed to %r'
                                 % (ans['id'], prior.get('value'), ans.get('value')))
            raise ValueError('unknown gap id: %r' % (ans.get('id'),))
        value = ans.get('value')
        el = els.get(g['element']) if g['element'] else None
        record = {'gap': g['id'], 'element': g['element'], 'field': g['field'],
                  'value': value, 'because': ans.get('because', ''),
                  'answered_by': ans.get('answered_by', 'agent')}
        if el is None:
            applied.append(record)
            continue
        if g['field'] == 'count':
            el['count'] = value or 1
        elif value is None:
            dropped.add(el['id'])
            record['effect'] = 'element omitted'
        else:
            el[g['field']] = value
            el['fields'][g['field']] = 'assumed'
            if g['field'] == 'level':
                el['level_depth_m'] = _level_depth(spec, value)
            if g['field'] == 'bearing_deg':
                el['bearing_precision'] = 'stated'
        el['confidence'] = _confidence(el)
        applied.append(record)
    answered = set(a['gap'] for a in applied)
    spec['elements'] = [e for e in spec['elements'] if e['id'] not in dropped]
    kept = set(e['id'] for e in spec['elements'])
    spec['gaps'] = [g for g in spec['gaps']
                    if g['id'] not in answered and (g['element'] is None or g['element'] in kept)]
    spec['answers'] = (spec.get('answers') or []) + applied
    spec['coverage']['unresolved'] = sum(1 for g in spec['gaps'] if g['required'])
    spec['coverage']['questions'] = len(spec['gaps'])
    spec['coverage']['elements'] = len(spec['elements'])
    return spec


def _level_depth(spec, label):
    for e in spec['elements']:
        if e.get('level') == label and e.get('level_depth_m') is not None:
            return e['level_depth_m']
    try:
        return _f(label) * FT
    except (TypeError, ValueError):
        return None


def parse_bearing(text):
    """``(azimuth_deg, precision)`` for the first bearing in ``text``, or None.
    Public because the assay reader needs the same grammar for vein strikes."""
    got = _pull_bearing(text, _Mask(), 0)
    return (got[0], got[1]) if got else None


def parse_dip(text):
    """Dip in degrees below horizontal for the first one stated, or None."""
    got = _pull_dip(text, _Mask(), 0)
    return got[0] if got else None


def unresolved(spec):
    return [g for g in spec['gaps'] if g['required']]
