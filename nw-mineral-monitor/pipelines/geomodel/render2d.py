"""geomodel.render2d — plan, section and isometric views as plain SVG.

Standard library only: no matplotlib, no numpy.  The output is deterministic —
same model in, same bytes out — because the published views are content
addressed alongside the model they illustrate.

The line style is the honesty mechanism, and it is not decorative:

    surveyed   solid      traced off a georeferenced plan
    described  dashed     read off a written description
    assumed    dotted     supplied in answer to a question the text left open

A described adit must never be able to pass for a surveyed one at a glance, so
the legend states the counts as well, and the subtitle says in words that the
drawing came from a description.
"""
import math

from . import workings as wk

RENDERER_VERSION = 'nwmm-render2d/1'

W, H = 960, 700
PAD = 56

DASH = {'surveyed': None, 'sketched': '10 5', 'inferred': '2 5',
        'described': '10 5', 'assumed': '2 5'}

ASSAY = '#b8860b'
VEIN = '#1f9d72'
INK = '#1b1b1b'
MUTED = '#7a7a7a'
PAPER = '#ffffff'
CONTOUR = '#c9c2b4'

#: the drawable rectangle: below the title block, above the scale bar
PLOT = (PAD - 24.0, 56.0, W - PAD + 24.0, H - 34.0)


# ------------------------------------------------------------------ helpers
def _esc(s):
    return (str(s).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
            .replace('"', '&quot;'))


def _n(v):
    """Fixed formatting so two renders of one model are byte-identical."""
    return ('%.2f' % (v + 0.0)).rstrip('0').rstrip('.') or '0'


def _rgb(c):
    return '#%02x%02x%02x' % tuple(int(max(0, min(255, v))) for v in (c or [140, 140, 140]))


class View(object):
    """Maps model coordinates to SVG user units, y flipped, aspect preserved."""

    def __init__(self, pts, width=W, height=H, pad=PAD):
        xs = [p[0] for p in pts] or [0.0]
        ys = [p[1] for p in pts] or [0.0]
        self.minx, self.maxx = min(xs), max(xs)
        self.miny, self.maxy = min(ys), max(ys)
        # "The shaft was sunk 300 feet" is a whole, common description, and in plan
        # it is a single point.  Flooring a zero span at 1e-6 m made the scale
        # hundreds of millions of pixels per metre, so the scale bar was drawn a
        # third of a billion units wide and labelled "1 m".  Give a degenerate
        # extent a real span around its centre instead, and everything downstream
        # — bar, placement, centring — comes out at a readable scale.
        self.minx, self.maxx = _span(self.minx, self.maxx)
        self.miny, self.maxy = _span(self.miny, self.maxy)
        dx = self.maxx - self.minx
        dy = self.maxy - self.miny
        self.k = min((width - 2 * pad) / dx, (height - 2 * pad) / dy)
        self.ox = pad + ((width - 2 * pad) - dx * self.k) / 2.0
        self.oy = pad + ((height - 2 * pad) - dy * self.k) / 2.0
        self.height = height

    def __call__(self, x, y):
        return (self.ox + (x - self.minx) * self.k,
                self.height - (self.oy + (y - self.miny) * self.k))


# ------------------------------------------------------------- model reading
def _parts(built):
    """[(kind, name, confidence, [(x, y, z), ...])] in a stable order."""
    ws = built['workings']
    out = []
    for k, feat in enumerate(ws.features):
        out.append((feat.get('type', 'unknown'), feat.get('name', ''),
                    feat.get('confidence', 'described'), list(ws.part_xyz(k))))
    for mesh in built['project'].by_kind('mesh'):
        if getattr(mesh, 'role', '') != 'stope':
            continue
        md = mesh.metadata
        n = mesh.n_vertices // 2
        ring = [mesh.vertex(i) for i in range(n)] + [mesh.vertex(0)]
        out.append(('stope', md.get('name', ''), md.get('confidence', 'described'), ring))
    return out


def _dominant_bearing(built):
    """Length-weighted circular mean of the horizontal workings' strikes,
    folded to 0–180 so a drift and its back-azimuth agree."""
    sx = sy = 0.0
    for kind, _, _, pts in _parts(built):
        for a, b in zip(pts, pts[1:]):
            dx, dy, dz = b[0] - a[0], b[1] - a[1], b[2] - a[2]
            h = math.hypot(dx, dy)
            if h < 1e-6 or abs(dz) > h:
                continue
            ang = 2.0 * math.atan2(dx, dy)
            sx += h * math.cos(ang)
            sy += h * math.sin(ang)
    if sx == 0.0 and sy == 0.0:
        return 0.0
    return round(math.degrees(math.atan2(sy, sx)) / 2.0 % 180.0, 4)


def _tally(built):
    counts = {}
    for _, _, conf, _ in _parts(built):
        counts[conf] = counts.get(conf, 0) + 1
    return counts


# --------------------------------------------------------------- decorations
#: a plan with no horizontal extent still has to be drawn at some scale; 20 m
#: puts a lone collar in the middle of a sheet you can measure off.
DEGENERATE_SPAN_M = 20.0


def _span(lo, hi):
    """Widen a degenerate extent about its centre so the scale stays finite."""
    if hi - lo >= 1e-3:
        return lo, hi
    mid = (lo + hi) / 2.0
    return mid - DEGENERATE_SPAN_M / 2.0, mid + DEGENERATE_SPAN_M / 2.0


def _nice(span):
    """A 1/2/5 × 10ⁿ bar length that fits comfortably inside the drawing."""
    target = span / 4.0
    if target <= 0:
        return 1.0
    e = math.floor(math.log10(target))
    for mult in (1.0, 2.0, 5.0, 10.0):
        v = mult * 10 ** e
        if v >= target:
            return v
    return 10 ** (e + 1)


def scale_bar(view, x, y):
    """Bar of a round number of metres, labelled in metres and feet."""
    metres = _nice((view.maxx - view.minx))
    px = metres * view.k
    ft = metres / wk.FT
    label = '%s m  ·  %s ft' % (_trim(metres), _trim(round(ft, -1) if ft >= 100 else round(ft)))
    return ('<g class="scale">'
            '<line x1="%s" y1="%s" x2="%s" y2="%s" stroke="%s" stroke-width="2"/>'
            '<line x1="%s" y1="%s" x2="%s" y2="%s" stroke="%s" stroke-width="2"/>'
            '<line x1="%s" y1="%s" x2="%s" y2="%s" stroke="%s" stroke-width="2"/>'
            '<text x="%s" y="%s" font-size="11" fill="%s">%s</text></g>'
            % (_n(x), _n(y), _n(x + px), _n(y), INK,
               _n(x), _n(y - 4), _n(x), _n(y + 4), INK,
               _n(x + px), _n(y - 4), _n(x + px), _n(y + 4), INK,
               _n(x), _n(y - 8), INK, _esc(label)))


def _trim(v):
    return ('%.10g' % v)


def north_arrow(x, y):
    return ('<g class="north">'
            '<path d="M %s %s L %s %s L %s %s Z" fill="%s"/>'
            '<text x="%s" y="%s" font-size="11" text-anchor="middle" fill="%s">N</text></g>'
            % (_n(x), _n(y - 22), _n(x - 7), _n(y), _n(x + 7), _n(y), INK,
               _n(x), _n(y + 14), INK))


def legend(built, x, y):
    """Line-style key plus the counts, so the reader is told in numbers as well
    as in ink how much of the drawing came from prose."""
    counts = _tally(built)
    rows = []
    for conf in ('surveyed', 'described', 'assumed'):
        n = counts.get(conf, 0)
        dash = DASH.get(conf)
        rows.append((conf, n, dash))
    parts = ['<g class="legend">']
    for i, (conf, n, dash) in enumerate(rows):
        yy = y + i * 17
        parts.append('<line x1="%s" y1="%s" x2="%s" y2="%s" stroke="%s" stroke-width="2"%s/>'
                     % (_n(x), _n(yy), _n(x + 28), _n(yy), INK,
                        ' stroke-dasharray="%s"' % dash if dash else ''))
        parts.append('<text x="%s" y="%s" font-size="11" fill="%s">%s — %d</text>'
                     % (_n(x + 34), _n(yy + 4), INK, _esc(conf), n))
    other = sum(v for k, v in counts.items() if k not in ('surveyed', 'described', 'assumed'))
    if other:
        parts.append('<text x="%s" y="%s" font-size="11" fill="%s">other — %d</text>'
                     % (_n(x + 34), _n(y + 3 * 17 + 4), INK, other))
    parts.append('</g>')
    return ''.join(parts)


def _frame(title, subtitle, body, width=W, height=H):
    return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 %d %d" width="%d" height="%d" '
            'role="img" aria-label="%s">'
            '<rect width="%d" height="%d" fill="%s"/>'
            '<text x="%d" y="28" font-size="16" font-family="Georgia,serif" fill="%s">%s</text>'
            '<text x="%d" y="46" font-size="11" fill="%s">%s</text>'
            '%s</svg>'
            % (width, height, width, height, _esc(title), width, height, PAPER,
               PAD - 24, INK, _esc(title), PAD - 24, MUTED, _esc(subtitle), body))


def _polyline(screen_pts, kind, conf, closed=False):
    if len(screen_pts) < 2:
        return ''
    pts = ' '.join('%s,%s' % (_n(a), _n(b)) for a, b in screen_pts)
    dash = DASH.get(conf)
    tag = 'polygon' if closed else 'polyline'
    fill = _rgb(wk.TYPES.get(kind, {}).get('color')) + '" fill-opacity="0.18' if closed else 'none'
    return ('<%s points="%s" fill="%s" stroke="%s" stroke-width="2.2" stroke-linejoin="round"%s/>'
            % (tag, pts, fill, _rgb(wk.TYPES.get(kind, {}).get('color')),
               ' stroke-dasharray="%s"' % dash if dash else ''))


def _labels(view, parts, gap=13.0):
    """Names on their own lines rather than all at the collar: a label sits at
    the middle of the working it names, and anything still overlapping is
    nudged up until it does not."""
    placed, out = [], []
    for kind, name, conf, ps in parts:
        if not name or not ps:
            continue
        mx = (ps[0][0] + ps[-1][0]) / 2.0
        my = (ps[0][1] + ps[-1][1]) / 2.0
        x, y = view(mx, my)
        x, y = x + 5.0, y - 5.0
        while any(abs(x - px) < 46.0 and abs(y - py) < gap for px, py in placed):
            y -= gap
        placed.append((x, y))
        out.append('<text x="%s" y="%s" font-size="10" fill="%s">%s</text>'
                   % (_n(x), _n(y), INK, _esc(name)))
    return ''.join(out)


def _assay_points(built):
    """[(x, y, z, commodity, value, basis)] for the quoted grades, if any."""
    for ps in built['project'].by_kind('points'):
        if (ps.metadata or {}).get('schema') != 'nwmm-assay/1':
            continue
        cols = ps.attributes
        out = []
        for i in range(ps.n):
            x, y, z = ps.point(i)
            out.append((x, y, z, cols.get('commodity', [])[i], cols.get('value', [])[i],
                        cols.get('basis', [])[i]))
        return out
    return []


def _assays(built, to_screen):
    """Grade points.  A selected sample is drawn hollow and a representative
    one filled, for the same reason a described adit is drawn dashed: the two
    are different claims and must not look alike."""
    pts = _assay_points(built)
    if not pts:
        return ''
    out = ['<g class="assays">']
    for x, y, z, commodity, value, basis in pts:
        sx, sy = to_screen((x, y, z))
        picked = basis == 'selected'
        out.append('<circle cx="%s" cy="%s" r="4" fill="%s" stroke="%s" stroke-width="1.4"%s/>'
                   % (_n(sx), _n(sy), 'none' if picked else ASSAY, ASSAY,
                      ' stroke-dasharray="2 2"' if picked else ''))
        label = '%.4g %s' % (value, (commodity or '?').upper())
        out.append('<text x="%s" y="%s" font-size="9" fill="%s">%s</text>'
                   % (_n(sx + 7), _n(sy + 3), ASSAY, _esc(label)))
    out.append('</g>')
    return ''.join(out)


def _assay_key(built, x, y):
    if not _assay_points(built):
        return ''
    return ('<g class="assay-key">'
            '<circle cx="%s" cy="%s" r="4" fill="%s"/>'
            '<text x="%s" y="%s" font-size="10" fill="%s">representative</text>'
            '<circle cx="%s" cy="%s" r="4" fill="none" stroke="%s" stroke-width="1.4" '
            'stroke-dasharray="2 2"/>'
            '<text x="%s" y="%s" font-size="10" fill="%s">selected sample</text></g>'
            % (_n(x), _n(y), ASSAY, _n(x + 9), _n(y + 3), INK,
               _n(x + 108), _n(y), ASSAY, _n(x + 117), _n(y + 3), INK))


def _vein_trace(built, view):
    """The described vein attitude as its strike line through the workings."""
    vein = built.get('vein')
    if not vein:
        return ''
    pts = [p for _, _, _, ps in _parts(built) for p in ps]
    if not pts:
        return ''
    cx = sum(p[0] for p in pts) / len(pts)
    cy = sum(p[1] for p in pts) / len(pts)
    half = max(80.0, max(max(p[0] for p in pts) - min(p[0] for p in pts),
                         max(p[1] for p in pts) - min(p[1] for p in pts)) / 2.0)
    b = math.radians(vein['strike_deg'])
    a = view(cx - half * math.sin(b), cy - half * math.cos(b))
    c = view(cx + half * math.sin(b), cy + half * math.cos(b))
    return ('<g class="vein"><line x1="%s" y1="%s" x2="%s" y2="%s" stroke="%s" '
            'stroke-width="1.6" stroke-dasharray="9 3 2 3"/>'
            '<text x="%s" y="%s" font-size="10" fill="%s">vein %03.0f\u00b0 / %.0f\u00b0%s</text></g>'
            % (_n(a[0]), _n(a[1]), _n(c[0]), _n(c[1]), VEIN,
               _n(c[0] + 5), _n(c[1]), VEIN, vein['strike_deg'], vein['dip_deg'],
               ' (dip direction assumed)' if vein.get('dip_direction_assumed') else ''))


def _flat(ps):
    """True when a part has no horizontal extent worth drawing (< 1 m)."""
    xs = [p[0] for p in ps]
    ys = [p[1] for p in ps]
    return max(xs) - min(xs) < 1.0 and max(ys) - min(ys) < 1.0


def _collar(view, p, kind, conf):
    x, y = view(p[0], p[1])
    dash = DASH.get(conf)
    return ('<rect x="%s" y="%s" width="9" height="9" fill="none" stroke="%s" stroke-width="2.2"%s/>'
            % (_n(x - 4.5), _n(y - 4.5), _rgb(wk.TYPES.get(kind, {}).get('color')),
               ' stroke-dasharray="%s"' % dash if dash else ''))


def _subtitle(built, extra=''):
    mine = built['project'].name
    bits = ['%s — workings digitised from a written description, not a survey' % mine]
    if extra:
        bits.append(extra)
    return ' · '.join(bits)


# -------------------------------------------------------------------- views
def plan(built, contours=True):
    """Map view: workings over a contour skeleton when terrain is present."""
    parts = _parts(built)
    pts = [(p[0], p[1]) for _, _, _, ps in parts for p in ps]
    if not pts:
        return _frame('Plan', _subtitle(built, 'no placeable workings'), '')
    view = View(pts)
    body = []
    if contours:
        body.append(_contours(built, view))
    for kind, name, conf, ps in parts:
        if _flat(ps):
            # a vertical shaft or winze is a point in plan; drawing it as a
            # zero-length line would make it vanish, so it gets the collar
            # square that mine plans have always used for one
            body.append(_collar(view, ps[0], kind, conf))
        else:
            body.append(_polyline([view(p[0], p[1]) for p in ps], kind, conf,
                                  closed=(kind == 'stope')))
    body.append(_vein_trace(built, view))
    body.append(_assays(built, lambda p: view(p[0], p[1])))
    body.append(_labels(view, parts))
    body.append(north_arrow(W - PAD, PAD + 6))
    body.append(scale_bar(view, PAD - 24, H - 24))
    body.append(legend(built, W - 190, H - 76))
    body.append(_assay_key(built, PAD - 24, H - 46))
    return _frame('Plan', _subtitle(built), ''.join(body))


def section(built):
    """Longitudinal section on the dominant strike, with the levels drawn in."""
    az = _dominant_bearing(built)
    a = math.radians(az)
    parts = _parts(built)
    proj = [(kind, name, conf, [(p[0] * math.sin(a) + p[1] * math.cos(a), p[2]) for p in ps])
            for kind, name, conf, ps in parts]
    pts = [p for _, _, _, ps in proj for p in ps]
    if not pts:
        return _frame('Longitudinal section', _subtitle(built, 'no placeable workings'), '')
    view = View(pts)
    body = []
    for label, z in sorted((built.get('levels') or {}).items(), key=lambda kv: -kv[1]):
        _, sy = view(0.0, z)
        body.append('<line x1="%s" y1="%s" x2="%s" y2="%s" stroke="%s" stroke-width="1" '
                    'stroke-dasharray="4 4"/>' % (_n(PAD - 20), _n(sy), _n(W - PAD + 20), _n(sy), MUTED))
        body.append('<text x="%s" y="%s" font-size="10" fill="%s">%s level · %s m</text>'
                    % (_n(PAD - 18), _n(sy - 4), MUTED, _esc(label), _n(z)))
    for kind, name, conf, ps in proj:
        body.append(_polyline([view(s, z) for s, z in ps], kind, conf,
                              closed=(kind == 'stope')))
    body.append(_assays(built, lambda p: view(p[0] * math.sin(a) + p[1] * math.cos(a), p[2])))
    body.append(scale_bar(view, PAD - 24, H - 24))
    body.append(legend(built, W - 190, H - 76))
    body.append(_assay_key(built, PAD - 24, H - 46))
    return _frame('Longitudinal section', _subtitle(built, 'looking along %03.0f°' % az), ''.join(body))


def iso(built):
    """Axonometric of the whole workings set: 30° dimetric, Z up."""
    c, s = math.cos(math.radians(30.0)), math.sin(math.radians(30.0))
    parts = _parts(built)
    proj = [(kind, name, conf, [((p[0] - p[1]) * c, (p[0] + p[1]) * s * 0.5 + p[2]) for p in ps])
            for kind, name, conf, ps in parts]
    pts = [p for _, _, _, ps in proj for p in ps]
    if not pts:
        return _frame('Isometric', _subtitle(built, 'no placeable workings'), '')
    view = View(pts)
    body = [_polyline([view(x, y) for x, y in ps], kind, conf, closed=(kind == 'stope'))
            for kind, name, conf, ps in proj]
    body.append(legend(built, W - 190, H - 76))
    body.append('<text x="%s" y="%s" font-size="10" fill="%s">vertical exaggeration 1:1 · '
                'no scale bar (an isometric has no single scale)</text>'
                % (_n(PAD - 24), _n(H - 24), MUTED))
    return _frame('Isometric', _subtitle(built), ''.join(body))


# ----------------------------------------------------------------- contours
def _contours(built, view, target=12):
    """Marching-squares skeleton over the topography grid, when there is one.
    With no terrain in the project this draws nothing rather than a fiction."""
    grids = [g for g in built['project'].by_kind('grid2d')
             if getattr(g, 'role', '') == 'topography']
    if not grids:
        return ''
    g = grids[0]
    lo, hi = g.zrange()
    if lo is None or hi is None or not (hi > lo):
        return ''
    step = _nice((hi - lo) * 4.0 / target) or 1.0
    segs = []
    level = math.ceil(lo / step) * step
    while level < hi:
        segs.extend(_march(g, level))
        level += step
    if not segs:
        return ''
    # The view is fitted to the workings, but the terrain grid covers the whole
    # site box, so most contour segments land outside the drawing.  Clip them:
    # it keeps them out of the title and legend margins, and stops the file
    # carrying thousands of invisible lines.
    out = ['<g class="contours">']
    for (x0, y0), (x1, y1) in segs:
        a, b = view(x0, y0), view(x1, y1)
        seg = _clip(a[0], a[1], b[0], b[1], PLOT)
        if seg is None:
            continue
        out.append('<line x1="%s" y1="%s" x2="%s" y2="%s" stroke="%s" stroke-width="0.7"/>'
                   % (_n(seg[0]), _n(seg[1]), _n(seg[2]), _n(seg[3]), CONTOUR))
    out.append('</g>')
    return ''.join(out) if len(out) > 2 else ''


def _clip(x0, y0, x1, y1, box):
    """Liang-Barsky segment clip; ``None`` when the segment misses the box."""
    xmin, ymin, xmax, ymax = box
    dx, dy = x1 - x0, y1 - y0
    t0, t1 = 0.0, 1.0
    for p, q in ((-dx, x0 - xmin), (dx, xmax - x0), (-dy, y0 - ymin), (dy, ymax - y0)):
        if p == 0.0:
            if q < 0.0:
                return None
            continue
        r = q / p
        if p < 0.0:
            if r > t1:
                return None
            t0 = max(t0, r)
        else:
            if r < t0:
                return None
            t1 = min(t1, r)
    return (x0 + t0 * dx, y0 + t0 * dy, x0 + t1 * dx, y0 + t1 * dy)


def _march(g, level):
    """One contour level over a Grid2D, as unordered segments."""
    segs = []
    for j in range(g.ny - 1):
        for i in range(g.nx - 1):
            vs = [g.get(i, j), g.get(i + 1, j), g.get(i + 1, j + 1), g.get(i, j + 1)]
            if any(v != v for v in vs):                      # NaN: no data here
                continue
            xs = [g.node_xy(i, j), g.node_xy(i + 1, j), g.node_xy(i + 1, j + 1), g.node_xy(i, j + 1)]
            crossings = []
            for e in range(4):
                a, b = vs[e], vs[(e + 1) % 4]
                if (a < level) == (b < level):
                    continue
                t = (level - a) / (b - a) if b != a else 0.5
                pa, pb = xs[e], xs[(e + 1) % 4]
                crossings.append((pa[0] + t * (pb[0] - pa[0]), pa[1] + t * (pb[1] - pa[1])))
            if len(crossings) == 2:
                segs.append((crossings[0], crossings[1]))
            elif len(crossings) == 4:                        # saddle: join in order
                segs.append((crossings[0], crossings[1]))
                segs.append((crossings[2], crossings[3]))
    return segs


def render(built, views=('plan', 'section', 'iso')):
    """``{'plan': '<svg…>', 'section': …, 'iso': …}`` in the requested order."""
    fns = {'plan': plan, 'section': section, 'iso': iso}
    return dict((v, fns[v](built)) for v in views if v in fns)
