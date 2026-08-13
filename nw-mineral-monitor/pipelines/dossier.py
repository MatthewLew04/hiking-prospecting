#!/usr/bin/env python3
"""WS3 — dossier builder for the AOI.

For every graded mine and every mining-claim case in the AOI, compile a
dossier: facts from our snapshots (each with source URL + retrieval date),
the web-scrub history, and prefilled research links for the sources that
sit behind interactive apps or bot walls (MLRS public reports, county
recorder, Secretary of State, Mindat, The Diggings, HathiTrust) — those are
LINKS BY DESIGN, not scrapes; see ASSUMPTIONS.md.

Output: site/data/dossiers/{aoi}.json
  {mines: {gradeIdx: dossier}, claims: {serial: dossier}}
Dossier = {title, facts: [{k,v,src,ret}], links: [{label,url,note}],
           history: [hit...], addresses_note}
"""
import json, os, re, sys, urllib.parse
from common import load_aoi, load_state, SITE, TODAY, write_json, trs_label

MLRS_EP = 'https://gis.blm.gov/nlsdb/rest/services/Mining_Claims/MiningClaims/MapServer'


def links_for(name, serial, aoi):
    q = urllib.parse.quote((name or '').strip())
    state_name = load_state(aoi['state'])['name']
    state = urllib.parse.quote(state_name)
    ser = urllib.parse.quote((serial or '').strip())
    L = []
    if serial:
        L.append({'label': 'MLRS serial register (search the serial)',
                  'url': 'https://reports.blm.gov/report/MLRS/95/Serial-Register-Page',
                  'note': f'Interactive BLM report app — search {serial}. Claimant names, '
                          'mailing addresses of record, location date, full actions history.'})
        L.append({'label': 'MLRS Virtual Public Room', 'url': 'https://mlrs.blm.gov/',
                  'note': 'Case lookup by serial; official record.'})
        L.append({'label': f'The Diggings — serial {serial}',
                  'url': f'https://thediggings.com/usa?q={ser}',
                  'note': 'Unofficial MLRS mirror; convenient claimant summaries.'})
    if name:
        L.append({'label': f'Mindat search "{name}"',
                  'url': f'https://www.mindat.org/search.php?search={q}',
                  'note': 'Mineralogy + locality write-ups (bot-walled; open in browser).'})
        L.append({'label': f'Chronicling America — "{name}"',
                  'url': f'https://www.loc.gov/collections/chronicling-america/?q={q}+{state}',
                  'note': 'Full-text newspapers 1770–1963.'})
        L.append({'label': f'Internet Archive full-text — "{name}"',
                  'url': f'https://archive.org/search?query=%22{q}%22&sin=TXT',
                  'note': 'Search INSIDE 150 Mining & Scientific Press volumes, 3,800+ '
                          'E&MJ issues, and thousands of mining reports (open, no wall).'})
        L.append({'label': f'HathiTrust full-text — "{name}"',
                  'url': f'https://babel.hathitrust.org/cgi/ls?q1={q}%20{state};a=srchls;anyall1=all',
                  'note': 'Second full-text corpus for MSP/E&MJ (bot-walled; open in browser).'})
        L.append({'label': 'SEC EDGAR company filings',
                  'url': f'https://www.sec.gov/edgar/search/#/q={q}',
                  'note': f'National company and full-text filing search prefilled with {name}.'})
        L.append({'label': 'SEDAR+ company filings',
                  'url': 'https://www.sedarplus.ca/',
                  'note': f'Canadian issuer and technical-report search; search {name}.'})
        L.append({'label': 'USGS NGMDB / MapView',
                  'url': f'https://ngmdb.usgs.gov/mapview/?q={q}',
                  'note': 'Geologic maps + bulletin citations for the district.'})
    rec = aoi.get('recorder') or {}
    if rec:
        L.append({'label': rec.get('name', 'County recorder'),
                  'url': rec.get('url'),
                  'note': (rec.get('note') or '') + ' Location notices, assessment-work '
                          'affidavits, quitclaims — names people and shows work actually done. '
                          f'{rec.get("phone", "")} · {rec.get("address", "")}'})
    if aoi.get('sos_business_search'):
        L.append({'label': f'{state_name} business-entity search (company claimants)',
                  'url': aoi['sos_business_search'],
                  'note': 'Registered agent + officers for LLC/corp claimants. Interactive app.'})
    return L


def run(aoi_key=None):
    aoi = load_aoi(aoi_key)
    k = aoi['key']
    st = aoi['state']
    x0, y0, x1, y1 = aoi['bbox']
    with open(os.path.join(SITE, 'data/grades/grades.json'),
              encoding='utf-8') as source:
        g = json.load(source)
    try:
        hist = json.load(open(os.path.join(SITE, f'data/history/{k}.json')))
    except FileNotFoundError:
        hist = {'byName': {}, 'msha': []}

    def canon(name):
        n = (name or '').lower()
        n = re.sub(r'\b(the|mine|mines|mining|claims?|group|lode|placer|prospect|'
                   r'property|tunnel|shaft|no\.?\s*\d+|#\s*\d+|\d+)\b', ' ', n)
        n = re.sub(r'[^a-z ]+', ' ', n)
        return re.sub(r'\s+', ' ', n).strip()

    def hist_for(name):
        h = hist['byName'].get(canon(name))
        return h['hits'] if h else []

    def msha_for(name):
        cn = canon(name)
        return [m for m in hist.get('msha', []) if cn and canon(m.get('name')) == cn]

    F = lambda kk, v, src: {'k': kk, 'v': v, 'src': src, 'ret': TODAY}

    mines = {}
    for i in range(g['n']):
        if g['st'][i] != st or g['x'][i] is None: continue
        if not (x0 <= g['x'][i] <= x1 and y0 <= g['y'][i] <= y1): continue
        name = g['name'][i]
        facts = []
        gsrc = g['url'][i] or g['src'][i]
        if g['au'][i] is not None: facts.append(F('Gold grade', f"{g['au'][i]} oz/ton — “{g['quote'][i]}”", gsrc))
        if g['ag'][i] is not None: facts.append(F('Silver grade', f"{g['ag'][i]} oz/ton", gsrc))
        if g['ton'][i]: facts.append(F('Tonnage', g['ton'][i], gsrc))
        if g['ds'][i]: facts.append(F('MRDS status', g['ds'][i] + (f" · {g['pz'][i]} producer" if g['pz'][i] else ''), 'USGS MRDS'))
        if g['wt'][i]: facts.append(F('Workings', g['wt'][i], 'USGS MRDS'))
        if g['com'][i]: facts.append(F('Commodities', g['com'][i], 'USGS MRDS'))
        if g['cnty'][i]: facts.append(F('County', g['cnty'][i], 'USGS MRDS'))
        if g['open'][i] is not None and g['open'][i] >= 0:
            facts.append(F('Nearest active claim',
                           '>5 km' if g['open'][i] >= 5000 else f"{g['open'][i]} m",
                           'BLM MLRS snapshot 2026-07-30'))
        for m in msha_for(name):
            facts.append(F('MSHA mine ID', f"{m['mine_id']} — {m.get('operator') or '?'} "
                           f"({m.get('status') or '?'} {m.get('status_dt') or ''})", m['src']))
        mines[str(i)] = {'title': name, 'facts': facts,
                         'links': links_for(name, None, aoi),
                         'history': hist_for(name)}

    # Claims: NO compiled dossiers on disk — 6,500+ cases × boilerplate links
    # ballooned to 17 MB. The client builds claim dossiers live from
    # {aoi}_claims.json (facts) + history/{aoi}.json (newspaper hits) + a JS
    # twin of links_for(). Only the recorder/SoS config rides along here.
    out = {'aoi': k, 'generated': TODAY,
           'addresses_note': ('Claimant names/addresses live in the MLRS serial register '
                              '(link on every claim). Addresses of record go stale — the '
                              'register shows the filing date; treat anything >5 years old '
                              'as historical. Public records only.'),
           'recorder': aoi.get('recorder'), 'sos': aoi.get('sos_business_search'),
           'mines': mines}
    write_json(f'data/dossiers/{k}.json', out)
    print(f'dossiers: {len(mines)} mines (claims render client-side)')
    return out


if __name__ == '__main__':
    run(sys.argv[1] if len(sys.argv) > 1 else None)
