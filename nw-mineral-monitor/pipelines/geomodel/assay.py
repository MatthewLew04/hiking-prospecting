"""geomodel.assay — the grades a description quotes, and the vein it describes.

The prose that says where a working goes usually also says what came out of
it: "the ore averaged 0.5 ounce gold to the ton across 3 feet", "$19.14 a
ton", "12 per cent lead".  This module reads those figures, ties each one to
the working whose sentence it appeared in, and turns them into grade points
sitting on that working — so the model shows not just where the mine went but
what it was following.

Two things are recorded that matter more than the number itself:

* **the basis.**  "Selected samples assayed 40 ounces" and "the mill heads
  averaged 0.4 ounce" are not the same claim, and a model that flattened them
  into one number would be lying about the deposit.  Picked, average and plain
  assay are kept apart, and a picked sample says so on its own point.
* **the width.**  A bonanza figure over eight inches is not a mining grade.
  When the text gives a width it travels with the value.

What is deliberately *not* built: an interpolated grade surface.  Kriging three
quoted sentences would manufacture a resource out of an anecdote.  A vein
surface is built only when the text states both a strike and a dip, because
then the surface is the document's claim and not the modeller's.
"""
import math
import re

from . import narrative
from .model import Mesh, PointSet, farray, iarray

ASSAY_VERSION = 'nwmm-assay/1'

FT = narrative.FT

#: metal word -> the column the site's grade bundle uses
COMMODITY = {
    'gold': 'au', 'au': 'au',
    'silver': 'ag', 'ag': 'ag',
    'lead': 'pb', 'pb': 'pb', 'galena': 'pb',
    'zinc': 'zn', 'zn': 'zn', 'sphalerite': 'zn',
    'copper': 'cu', 'cu': 'cu', 'chalcopyrite': 'cu',
    'antimony': 'sb', 'sb': 'sb', 'stibnite': 'sb',
    'tungsten': 'wo3', 'wo3': 'wo3', 'scheelite': 'wo3',
}

UNITS = {'au': 'oz/ton', 'ag': 'oz/ton', 'pb': '%', 'zn': '%', 'cu': '%',
         'sb': '%', 'wo3': '%', 'usd': '$/ton'}

_NUM = r'\d[\d,]*(?:\.\d+)?'

#: a dollar figure is a *grade* only when it is per ton.  "the group yielded
#: $1,000,000" is total production, and reading it as a grade would claim a
#: million dollars to the ton.
RE_DOLLAR = re.compile(r'\$\s*(?P<val>' + _NUM + r')\s*(?:to|per|a)\s+(?:the\s+)?ton\b', re.I)
RE_OZ = re.compile(r'(?P<val>' + _NUM + r')\s*(?:oz\.?|ounces?)\b', re.I)
RE_PCT = re.compile(r'(?P<val>' + _NUM + r')\s*(?:per\s*cent\.?|percent|%)', re.I)

RE_COMMODITY = re.compile(r'\b(?P<word>' + '|'.join(
    sorted(COMMODITY, key=len, reverse=True)) + r')\b', re.I)

RE_WIDTH = re.compile(
    r'(?:across|over|for)\s+(?:a\s+(?:width|thickness)\s+of\s+)?(?P<val>' + _NUM + r')'
    r'\s*(?P<unit>ft\.?|feet|foot|inch(?:es)?|in\.?|m\b|metres?|meters?)'
    r'|(?P<val2>' + _NUM + r')[\s-]*(?P<unit2>ft\.?|feet|foot|inch(?:es)?)[\s-]*'
    r'(?:wide|streak|stringer|seam|width|vein|shoot|ore\s+body)', re.I)

#: how the figure was arrived at — the difference between a resource and an anecdote
BASIS = (
    ('selected', re.compile(r'\b(select(?:ed)?|picked|hand-?sorted|bonanza|high[- ]?grade|'
                            r'specimen|richest|choice)\b', re.I)),
    ('average', re.compile(r'\b(average[sd]?|averaging|mean|mill\s+heads?|production)\b', re.I)),
    ('shipment', re.compile(r'\b(shipp?ed|shipment|carload|smelter\s+returns?)\b', re.I)),
)

RE_STRIKE = re.compile(r'\bstrik(?:es?|ing)\b[^.;]{0,40}', re.I)
RE_VEIN_DIP = re.compile(r'\bdip(?:s|ping|ped)?\b[^.;]{0,40}', re.I)
RE_VEIN = re.compile(r'\b(vein|lode|ledge|shoot|ore\s+body|orebody)\b', re.I)

#: percentages that are not ore grades
RE_NOT_A_GRADE = re.compile(
    r'\b(recover(?:y|ies|ed|ing)|extraction|extracted|moisture|dilution|royalt(?:y|ies)|'
    r'interest|discount|purity|efficien\w*|of\s+the\s+total|of\s+the\s+output)\b', re.I)

RE_ASSAY_CUE = re.compile(
    r'\b(assay|assays|assayed|averag|ran|carried|yielded|value[sd]?|ore|grade|'
    r'ton|oz|ounce|per\s*cent|percent)\b', re.I)


# ------------------------------------------------------------------ reading
def _f(v):
    return float(str(v).replace(',', ''))


def _basis(window):
    for name, pattern in BASIS:
        if pattern.search(window):
            return name
    return 'assay'


def _width_m(window):
    m = RE_WIDTH.search(window)
    if not m:
        return None
    val = _f(m.group('val') or m.group('val2'))
    unit = (m.group('unit') or m.group('unit2') or 'ft').lower().rstrip('.')
    if unit.startswith('in'):
        return round(val * FT / 12.0, 5)
    if unit in ('m', 'metre', 'metres', 'meter', 'meters'):
        return round(val, 5)
    return round(val * FT, 5)


def _nearest_commodity(text, at, reach=48):
    """The metal word closest to a figure, searching after it first because
    "0.5 ounce gold" is commoner than "gold, 0.5 ounce"."""
    after = text[at:at + reach]
    m = RE_COMMODITY.search(after)
    if m:
        return COMMODITY[m.group('word').lower()], m.start() + at
    before = text[max(0, at - reach):at]
    hits = list(RE_COMMODITY.finditer(before))
    if hits:
        m = hits[-1]
        return COMMODITY[m.group('word').lower()], max(0, at - reach) + m.start()
    return None, None


def parse(text, spec=None):
    """Assay figures in ``text``, tied to the elements of ``spec`` by sentence.

    Returns ``{'assays': [...], 'vein': {...}|None, 'gaps': [...]}``.
    """
    text = text or ''
    sents = narrative.sentences(text)
    # A figure is quoted for the working just described, and the two are often
    # separated by a semicolon rather than a full stop ("a drift was extended
    # 450 feet; the ore averaged 0.5 ounce").  Attaching by exact sentence
    # would drop those, so an assay takes the last working that began before
    # it, and the manifest records which one it landed on.
    anchors = sorted((tuple(el['span'])[0], el['id'])
                     for el in ((spec or {}).get('elements') or []) if el.get('span'))

    def owner(at):
        found = None
        for start, eid in anchors:
            if start <= at:
                found = eid
            else:
                break
        return found

    assays, gaps, taken = [], [], []

    def claim(a, b):
        if any(a < y and x < b for x, y in taken):
            return False
        taken.append((a, b))
        return True

    for start, end, body in sents:
        window = body
        basis = _basis(window)
        width = _width_m(window)
        for pattern, kind in ((RE_DOLLAR, 'usd'), (RE_PCT, 'pct'), (RE_OZ, 'oz')):
            for m in pattern.finditer(body):
                if not claim(start + m.start(), start + m.end()):
                    continue
                if kind == 'pct' and RE_NOT_A_GRADE.search(body[max(0, m.start() - 40):m.start()]):
                    continue                      # a recovery, not an ore grade
                value = _f(m.group('val'))
                if kind == 'usd':
                    commodity, unit = 'usd', '$/ton'
                else:
                    commodity, _ = _nearest_commodity(body, m.end())
                    unit = 'oz/ton' if kind == 'oz' else '%'
                assays.append({
                    'id': None,
                    'at': start + m.start(),
                    'commodity': commodity,
                    'value': value,
                    'unit': unit,
                    'width_m': width,
                    'basis': basis,
                    'element': owner(start + m.start()),
                    'confidence': 'described',
                    'quote': re.sub(r'\s+', ' ', body).strip(),
                    'span': [start, end],
                })
    assays.sort(key=lambda a: a['at'])
    for i, a in enumerate(assays, 1):
        a['id'] = 'a%d' % i
        a.pop('at')
    for a in assays:
        if a['commodity'] is None:
            gaps.append({
                'element': a['element'], 'assay': a['id'], 'field': 'commodity',
                'required': False, 'kind': 'assay',
                'question': 'A value of %g %s is quoted but no metal is named. Which metal '
                            'was it?' % (a['value'], a['unit']),
                'quote': a['quote'], 'span': a['span'],
                'options': [{'value': c, 'label': c.upper()} for c in ('au', 'ag', 'pb', 'zn', 'cu')]
                           + [{'value': None, 'label': 'unknown — leave this assay out'}],
            })
    return {'assays': assays, 'vein': parse_vein(text), 'gaps': gaps,
            'version': ASSAY_VERSION}


def parse_vein(text):
    """Strike and dip of the vein, when the text states both.

    One without the other does not define a surface, so nothing is returned —
    a vein drawn at a guessed dip would be the modeller's invention wearing the
    document's authority.
    """
    text = text or ''
    if not RE_VEIN.search(text):
        return None
    strike = dip = dip_dir = None
    quote = ''
    for start, end, body in narrative.sentences(text):
        if not RE_VEIN.search(body):
            continue
        m = RE_STRIKE.search(body)
        if m and strike is None:
            got = narrative.parse_bearing(m.group(0))
            if got:
                strike, quote = got[0], re.sub(r'\s+', ' ', body).strip()
        m = RE_VEIN_DIP.search(body)
        if m and dip is None:
            got = narrative.parse_dip(m.group(0))
            if got:
                dip = got
                word = re.search(r'\b(north|south|east|west|northeast|northwest|southeast|'
                                 r'southwest)(?:erly|ward)?\b', m.group(0), re.I)
                if word:
                    dip_dir = narrative.QUADRANT_WORDS.get(word.group(1).lower())
                quote = quote or re.sub(r'\s+', ' ', body).strip()
    if strike is None or dip is None:
        return None
    if dip_dir is None:
        # dip direction is 90 deg clockwise of strike unless the text says
        # otherwise; recorded so the choice is visible rather than silent
        dip_dir = (strike + 90.0) % 360.0
        assumed = True
    else:
        assumed = False
    return {'strike_deg': round(strike % 360.0, 4), 'dip_deg': round(dip, 4),
            'dip_direction_deg': round(dip_dir % 360.0, 4),
            'dip_direction_assumed': assumed, 'confidence': 'described', 'quote': quote}


# --------------------------------------------------------------- attachment
def attach(spec, text=None):
    """Fold assays and the vein attitude into a parsed spec."""
    import json

    spec = json.loads(json.dumps(spec))
    body = text
    if body is None:
        body = spec.get('text')
    got = parse(body or '', spec)
    spec['assays'] = got['assays']
    spec['vein'] = got['vein']
    existing = list(spec.get('gaps') or [])
    existing.extend(got['gaps'])
    for i, g in enumerate(existing, 1):
        g['id'] = 'g%d' % i
    spec['gaps'] = existing
    spec['coverage']['assays'] = len(got['assays'])
    spec['coverage']['questions'] = len(existing)
    spec['coverage']['unresolved'] = sum(1 for g in existing if g['required'])
    return spec


# ------------------------------------------------------------------ objects
def grade_points(spec, placed, name='Assays (from description)'):
    """A PointSet of the quoted grades, each on the working it was quoted for.

    An assay whose sentence named no working sits at the collar; an assay whose
    metal was never named is left out rather than plotted as an unknown.
    """
    by_id = dict((p['element'], p) for p in placed)
    ps = PointSet(name=name, role='points', color=[255, 215, 90])
    ps.metadata['schema'] = 'nwmm-assay/1'
    for a in (spec.get('assays') or []):
        if a['commodity'] is None:
            continue
        rec = by_id.get(a['element'])
        if rec is None:
            continue
        x = (rec['start'][0] + rec['end'][0]) / 2.0
        y = (rec['start'][1] + rec['end'][1]) / 2.0
        z = (rec['start'][2] + rec['end'][2]) / 2.0
        ps.add(x, y, z, assay=a['id'], commodity=a['commodity'], value=a['value'],
               unit=a['unit'], width_m=a['width_m'], basis=a['basis'],
               element=a['element'], confidence=a['confidence'], quote=a['quote'])
    ps.provenance = {'source': 'grades quoted in the mine description',
                     'note': 'basis is carried per point: a selected sample is not an average'}
    return ps


def vein_surface(vein, placed, collar, extent=None, name='Vein (described attitude)'):
    """A plane at the described strike and dip, anchored on the workings.

    It is a *statement of attitude*, not a modelled surface: it is drawn flat,
    at the size of the workings, through their centre, and the manifest says
    where its anchor came from.
    """
    if not vein or not placed:
        return None
    xs = [v for p in placed for v in (p['start'][0], p['end'][0])]
    ys = [v for p in placed for v in (p['start'][1], p['end'][1])]
    zs = [v for p in placed for v in (p['start'][2], p['end'][2])]
    cx, cy, cz = sum(xs) / len(xs), sum(ys) / len(ys), sum(zs) / len(zs)
    half = extent or max(80.0, max(max(xs) - min(xs), max(ys) - min(ys), max(zs) - min(zs)) / 2.0)

    strike = math.radians(vein['strike_deg'])
    dip = math.radians(vein['dip_deg'])
    ddir = math.radians(vein['dip_direction_deg'])
    # along strike, and down dip
    sx, sy, sz = math.sin(strike), math.cos(strike), 0.0
    dx = math.sin(ddir) * math.cos(dip)
    dy = math.cos(ddir) * math.cos(dip)
    dz = -math.sin(dip)

    verts = farray()
    for u, v in ((-1, -1), (1, -1), (1, 1), (-1, 1)):
        verts.extend((cx + half * (u * sx + v * dx),
                      cy + half * (u * sy + v * dy),
                      cz + half * (u * sz + v * dz)))
    mesh = Mesh(verts, iarray([0, 1, 2, 0, 2, 3]), name=name, role='vein',
                color=[120, 255, 190])
    mesh.opacity = 0.35
    mesh.metadata.update({
        'schema': 'nwmm-assay-vein/1',
        'strike_deg': vein['strike_deg'], 'dip_deg': vein['dip_deg'],
        'dip_direction_deg': vein['dip_direction_deg'],
        'dip_direction_assumed': vein['dip_direction_assumed'],
        'confidence': vein['confidence'], 'quote': vein['quote'],
        'anchor': 'centre of the described workings',
        'note': 'a statement of the attitude the text gives, drawn at the size of the '
                'workings; it is not an interpolated or modelled vein surface',
    })
    mesh.provenance = {'source': 'strike and dip stated in the mine description'}
    return mesh


def plane_mesh(x, y, z, dip, dip_azimuth, half_strike=150.0, half_dip=150.0, role='vein', name=None,
               color=None, confidence='described', from_measurement=None, source=None, metadata=None,
               polarity=1):
    """A finite rectangle through (x, y, z) with a stated attitude — the JS
    ``planeMesh``: corners centre + u·half_strike·strike + v·half_dip·dip for
    (u, v) in (−1,−1), (1,−1), (1,1), (−1,1), the corner order of
    ``vein_surface`` above, and two triangles.  ``dip_azimuth`` is the
    down-dip direction clockwise from north (strike = dip_azimuth − 90).
    A statement of attitude, not a modelled surface; the metadata says so."""
    dip, dip_azimuth = float(dip), float(dip_azimuth)
    if not dip >= 0 or dip > 90:
        raise ValueError('dip must be 0..90 degrees below horizontal (got %r)' % dip)
    if not half_strike > 0 or not half_dip > 0:
        raise ValueError('the half extents along strike and down dip must be > 0')
    d, a = math.radians(dip), math.radians(dip_azimuth)
    s = (-math.cos(a), math.sin(a), 0.0)
    dv = (math.sin(a) * math.cos(d), math.cos(a) * math.cos(d), -math.sin(d))
    verts = farray()
    for u, v in ((-1, -1), (1, -1), (1, 1), (-1, 1)):
        verts.extend((x + half_strike * u * s[0] + half_dip * v * dv[0],
                      y + half_strike * u * s[1] + half_dip * v * dv[1],
                      z + half_strike * u * s[2] + half_dip * v * dv[2]))
    role = 'fault' if role == 'fault' else 'vein'
    mesh = Mesh(verts, iarray([0, 1, 2, 0, 2, 3]), role=role,
                name=name or '%s plane %d° → %d°' % (role, round(dip), round(dip_azimuth)),
                color=color or ([230, 90, 90] if role == 'fault' else [120, 255, 190]))
    mesh.opacity = 0.35
    mesh.metadata.update(metadata or {})
    mesh.metadata.update({
        'schema': 'nwmm-assay-vein/1', 'dip': dip, 'dip_azimuth': dip_azimuth, 'polarity': int(polarity),
        'from_measurement': from_measurement, 'confidence': confidence or 'described', 'source': source,
        'centre': [x, y, z], 'half_strike_m': half_strike, 'half_dip_m': half_dip,
        'note': 'statement of attitude, not a modelled surface',
    })
    mesh.provenance = {'method': 'plane drawn through a point at a stated dip / dip azimuth (plane_mesh)',
                       'source': 'typed attitude' if source is None else source}
    return mesh
