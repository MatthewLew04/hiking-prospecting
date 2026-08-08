#!/usr/bin/env python3
"""Build rows_igs_b11.json — WS9 round-2 rows from IBMG Bulletin 11
(Piper & Laney 1926, Silver City region). Quotes are extracted verbatim
from the cached page text between unique start/end markers; hyphenated
line breaks joined, whitespace collapsed (the only sanctioned edits)."""
import gzip, json, re, sys

sys.path.insert(0, '/home/claude/nw/pipelines')
import gradeslib as G

d = json.load(gzip.open('/home/claude/nw/pipelines/cache/pagetext/igs_b11.json.gz'))
PAGES = d['pages']

def _find(t, marker, pos=0):
    """Whitespace-flexible search: marker tokens may be split across lines."""
    pat = r'\s+'.join(re.escape(w) for w in marker.split())
    m = re.compile(pat).search(t, pos)
    return m

def quote(pdf, start, end):
    t = PAGES[pdf - 1]
    ms = _find(t, start)
    assert ms, f'start marker not found pdf {pdf}: {start[:50]!r}'
    me = _find(t, end, ms.start())
    assert me, f'end marker not found pdf {pdf}: {end[:50]!r}'
    s = t[ms.start():me.end()]
    s = re.sub(r'-\s*\n\s*', '', s)      # join printed hyphenation (as validator does)
    s = re.sub(r'\s+', ' ', s).strip()   # collapse whitespace
    return s

SRC = 'igs_b11'
BASE = dict(state='ID', county='Owyhee', src_key=SRC, lat=None, lon=None)

rows = []
def add(name, district, keys, page, pdf, q_start, q_end, *, excl=[], metal='Au',
        au=None, ag=None, usd=None, pb=None, zn=None, cu=None, basis='value-text',
        years=None, price_year=None, tonnage=None, commodities='Gold, Silver',
        anchor=None):
    rows.append({
        'name': name, 'district': district, 'county': 'Owyhee', 'state': 'ID',
        'keys': keys, 'excl': excl, 'lat': None, 'lon': None,
        'anchor_hint': anchor or 'Silver City, Owyhee Co.',
        'metal': metal, 'au_opt': au, 'ag_opt': ag, 'au_gpt': None,
        'usd_per_ton': usd, 'pb_pct': pb, 'zn_pct': zn, 'cu_pct': cu,
        'sb_pct': None, 'wo3_units': None, 'hg_flasks': None,
        'usd_per_yd3': None, 'plc': None,
        'basis': basis, 'years': years, 'price_year': price_year,
        'tonnage': tonnage, 'commodities': commodities,
        'src_key': SRC, 'page': page, 'pdf_page': pdf,
        'quote': quote(pdf, q_start, q_end),
    })

SC = 'Silver City (Owyhee)'
DL = 'De Lamar (Owyhee)'
FL = 'Flint (Owyhee)'

# --- De Lamar mine (later company records / mill heads) ----------------------
add('De Lamar mine (pan-amalgamation mill ore, prior to 1897)', DL,
    ['de lamar', 'delamar'], 60, 35,
    ',4t D e Lamar prior t o 1897', 'naumannite, per ton.',
    au=0.85, ag=16.5, basis='production average', years='c. 1889-1897',
    price_year=1896, anchor='De Lamar, Owyhee Co.')

add("De Lamar mine ('cab' silver ore, third and fourth levels, late years)", DL,
    ['de lamar', 'delamar'], 109, 63,
    "I n t h e late years of the mine's activity", 'third and fourth levels.',
    metal='Ag', usd=35.0, basis='production', years='c. 1905-1914',
    price_year=1910, tonnage='a considerable tonnage',
    commodities='Silver', anchor='De Lamar, Owyhee Co.')

add('De Lamar mine (No. 9 vein, tenth-level stope, 1896)', DL,
    ['de lamar', 'delamar'], 110, 64,
    'I n the old mine, No. 9 ore body was opened', '7.6 ounces silver per ton.',
    au=1.13, ag=7.6, basis='production average', years='1896', price_year=1896,
    tonnage='ore body 365 ft long, avg width 3.0 ft',
    anchor='De Lamar, Owyhee Co.')

add('De Lamar mine (Sommercamp section vein, Gwinn 1920 estimate)', DL,
    ['de lamar', 'delamar'], 110, 64,
    'I n 1920, since', '11.4 ounces silver per ton.',
    au=0.025, ag=11.4, basis='resource estimate', years='1920', price_year=1920,
    tonnage='31,800 tons above the eighth level, avg width 6.8 ft',
    anchor='De Lamar, Owyhee Co.')

# --- Trade Dollar-Black Jack (Florida Mountain; 1903-1925 records) -----------
add('Trade Dollar mine (average value of ore mined, 1903-1909)', SC,
    ['trade dollar'], 124, 71,
    'The 8.yerage unit tot:l) value of ore mined',
    '<ible ore.',
    metal='Ag', usd=32.67, basis='production average', years='1903-1909',
    price_year=1906,
    tonnage='Trade Dollar Consolidated Mining Co. annual reports',
    anchor='Dewey, Florida Mountain, Owyhee Co.')

add('Trade Dollar mine (ore treated at Dewey mill)', SC,
    ['trade dollar'], 60, 35,
    'T h e ore treated contained 0.15', 'chalcopyrite in a quartz gangue.',
    au=0.15, ag=20.0, basis='production', years='1903-1910', price_year=1906,
    tonnage='mill feed; ranges 0.15-0.5 oz Au, 20-50 oz Ag (lower bounds recorded)',
    anchor='Dewey, Florida Mountain, Owyhee Co.')

add('Alpine vein, Trade Dollar mine (low-grade stope filling milled 1919)', SC,
    ['alpine', 'trade dollar'], 123, 70,
    'activity was limited to extracting',
    'and even less was handled the following year.',
    metal='Ag', usd=12.0, basis='production average', years='1919',
    price_year=1919, tonnage='2,125 tons milled in 1919',
    anchor='Dewey, Florida Mountain, Owyhee Co.')

add('Trade Dollar mine (first-class concentrate, Alpine vein, levels 11-12; not crude ore)', SC,
    ['trade dollar', 'alpine'], 120, 69,
    'Gold.. .', '0.7\n0.6',
    au=36.20, ag=1290.0, pb=0.8, zn=0.2, cu=0.9, basis='assay',
    years='c. 1925', price_year=1925,
    tonnage='concentrate analysis, not crude ore',
    anchor='Dewey, Florida Mountain, Owyhee Co.')

# --- Oro Fino group, War Eagle Mountain (Browne/Raymond/Mint + 1886 exam) ----
add('Oro Fino mine (average of tons mined prior to 1867)', SC,
    ['oro fino', 'orofino'], 140, 79,
    "In the 01'0 Fino, 80 tons", 'given as $27 per ton.',
    usd=27.0, basis='production average', years='pre-1867', price_year=1866,
    tonnage='2,050 tons mined prior to 1867',
    anchor='War Eagle Mountain, Silver City')

add("Oro Fino mine (lowest level, 4-ton lot, 1886 engineer's report)", SC,
    ['oro fino', 'orofino'], 141, 79,
    'from the surface, the ledge was !G feet wide', 'averaged $20 per ton.',
    usd=20.0, basis='assay', years='1886', price_year=1886,
    tonnage='4-ton lot across 16-ft ledge, 301 ft below surface',
    anchor='War Eagle Mountain, Silver City')

add('Ida Elmore mine (1872 production)', SC,
    ['ida elmore'], 141, 79,
    'The ore produced that same year averaged $44', 'for 779 tons.',
    usd=44.0, basis='production average', years='1872', price_year=1872,
    tonnage='779 tons', anchor='War Eagle Mountain, Silver City')

add('Golden Chariot mine (1872 production, shaft ~700 ft)', SC,
    ['golden chariot'], 141, 79,
    'The highest reported value per ton is $2G8', '700 feet deep.',
    usd=20.0, basis='production average', years='1872', price_year=1872,
    tonnage='5,965 tons in 1872', anchor='War Eagle Mountain, Silver City')

add('Mahogany mine (1871 production)', SC,
    ['mahogany'], 141, 79,
    'At the ;\\!ahogany', 'for 1,126 tons.',
    usd=50.08, basis='production average', years='1871', price_year=1871,
    tonnage='1,126 tons in 1871', anchor='War Eagle Mountain, Silver City')

add('Minnesota mine (War Eagle Mountain)', SC,
    ['minnesota'], 141, 79,
    'In the )1innesota the ore yielded', '$38 to $-14 per ton.',
    usd=38.0, basis='production', years='c. 1868-1875', price_year=1872,
    tonnage='range $38-$44; lower bound recorded',
    anchor='War Eagle Mountain, Silver City')

# --- Secondary War Eagle veins (Lindgren quoted in B-11) ---------------------
add('War Eagle shaft (Stormy Hill-Salvador vein, 1873)', SC,
    ['war eagle'], 148, 83,
    'This has been deve\\o[1erl to a depth of 700 feet', '$3.5 per ton.',
    excl=['consolidated'],
    usd=35.0, basis='production', years='1873', price_year=1873,
    tonnage="1873 production $21,698; printed value $35/ton (OCR '$3.5')",
    anchor='War Eagle Mountain, Silver City')

add('Illinois Central mine (1873 ore)', SC,
    ['illinois central'], 148, 83,
    'Nine hundred feet farther west is the Illinois Central',
    'aV~l\'aging $75 per too.',
    usd=73.0, basis='production', years='1873', price_year=1873,
    tonnage='1873 production $24,278',
    anchor='War Eagle Mountain, Silver City')

add('Silver Cord mine (first-class ore, 1867)', SC,
    ['silver cord', 'south poorman'], 138, 78,
    'Some of the first-cbss ore mined in 1867', 'rarely above $100 per ton.',
    metal='Ag', usd=2600.0, basis='ore shipped', years='1867', price_year=1867,
    tonnage='first-class (bonanza) lots; most of mine rarely above $100/ton',
    commodities='Silver, Gold', anchor='War Eagle Mountain, Silver City')

# --- Florida Mountain / Sinker Creek new properties --------------------------
add('Tip Top group (Florida Mountain)', SC,
    ['tip top'], 129, 73,
    'ft is reported locally that the Tip Top', 'average value of $16 per ton.',
    usd=16.0, basis='value-text', years='pre-1895', price_year=1893,
    tonnage='8,000 tons (reported locally)',
    anchor='Florida Mountain, Silver City')

add('Afterthought property (fourth level, Sinker Creek)', SC,
    ['afterthought'], 146, 82,
    'A body of ore developed on the first four levels',
    '17.0 ounces silver per ton on the fourth.',
    au=0.14, ag=17.0, basis='value-text', years='c. 1925', price_year=1925,
    tonnage='ore body 240 ft long on second level',
    anchor='Sinker Creek, War Eagle Mountain')

add('Never Sweat mine (upper-level ore body)', SC,
    ['never sweat'], 152, 85,
    'In the present upper level, an ore body', '92.8 ounces silver per ton.',
    au=0.40, ag=92.8, basis='value-text', years='c. 1923-1925', price_year=1925,
    tonnage='ore body 250 ft long, avg 17 in. wide',
    anchor='Stormy Hill, War Eagle Mountain')

add('Never Sweat mill concentrate (lot shipped August 1925; not crude ore)', SC,
    ['never sweat'], 62, 36,
    'August, 1925, assayed 58.88 ounces',
    'operators claim high recovery',
    au=58.88, ag=1573.0, basis='ore shipped', years='1925', price_year=1925,
    tonnage='flotation concentrate lot, not crude ore',
    anchor='Stormy Hill, War Eagle Mountain')

# --- Flint district (Precious Metals Mines Co. era, 1923-1925) ---------------
add('Rising Star mine (block mined below No. 4 level, Precious Metals Mines Co.)', FL,
    ['rising star'], 158, 88,
    'The block mined on the Ei:;ing Star vein', 'ounces sih\'er per ton.',
    metal='Ag', ag=20.0, basis='assay', years='1923-1925', price_year=1924,
    tonnage='assayed 20 to 30 oz Ag; lower bound recorded',
    commodities='Silver, Gold', anchor='Flint, Owyhee Co.')

add('Rising Star mill concentrate (400-ton run, c. 1924; not crude ore)', FL,
    ['rising star'], 157, 87,
    'rour hundred tQn:J', 'p-:r cent ("Opper',
    metal='Ag', au=1.0, ag=1000.0, cu=4.0, basis='production average',
    years='c. 1924', price_year=1924,
    tonnage='concentrate from 400 tons milled; 1-2 oz Au, 4-6% Cu (lower bounds)',
    commodities='Silver, Gold, Copper', anchor='Flint, Owyhee Co.')

add('Rising Star mill tailings (400-ton run, c. 1924)', FL,
    ['rising star'], 157, 87,
    'rour hundred tQn:J', 'p-:r cent ("Opper',
    metal='Ag', ag=3.0, basis='production average', years='c. 1924',
    price_year=1924, tonnage='tailings of 400 tons milled',
    commodities='Silver', anchor='Flint, Owyhee Co.')

add('Treasure Vault (Twilight) property, Birmingham group', FL,
    ['treasure vault', 'twilight'], 159, 88,
    "A bod~' of ore 3 1 :i to 5 feet wide", 'is exposed by the drift.',
    metal='Ag', usd=27.0, basis='value-text', years='c. 1925', price_year=1925,
    tonnage='ore body 3.5-5 ft wide, 105 ft long',
    commodities='Silver', anchor='Flint, Owyhee Co.')

add('Crescent prospect (rejected dump ore, Flint district)', FL,
    ['crescent'], 161, 89,
    'represents material rejected in the old days', 'but 110 gold.',
    metal='Ag', ag=40.9, basis='assay-text', years='c. 1925', price_year=1925,
    tonnage='average specimens of cobbing rejects on dump',
    commodities='Silver', anchor='Flint, Owyhee Co.')

add('Nellie Ann prospect (selected specimen)', FL,
    ['nellie ann'], 162, 90,
    'Grab samples of the pyritiferous quartz', '6.3 ounces silver per ton.',
    au=0.64, ag=6.3, basis='assay-text', years='c. 1925', price_year=1925,
    tonnage='grab samples 0.01-0.04 oz Au, 0.3-3.5 oz Ag; specimen recorded',
    anchor='Flint, Owyhee Co.')

# ---------------------------------------------------------------------------
out = '/home/claude/nw/grades-research/agent_out/rows_igs_b11.json'
json.dump(rows, open(out, 'w'), indent=1)
print(f'wrote {len(rows)} rows -> {out}')

# self-check every quote scores 1.0 on its cited pdf page
bad = 0
for r in rows:
    s = G.quote_on_page(r['quote'], PAGES[r['pdf_page'] - 1])
    if s < 1.0:
        bad += 1
        print(f"  score {s:.3f} :: {r['name']}")
print('all quotes exact' if not bad else f'{bad} quotes below 1.0')
