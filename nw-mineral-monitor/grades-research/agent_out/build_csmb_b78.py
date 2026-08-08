#!/usr/bin/env python3
"""Build rows_csmb_b78.json — quotes cut verbatim from cached page text.

Each row spec: pdf (1-based), start/end snippets in whitespace-collapsed
page text, drops = OCR column-scramble interjections removed (sanctioned
mid-quote artifacts), joins = hyphenated line-break joins.
"""
import json, gzip, re, sys, os
sys.path.insert(0, '/home/claude/nw/pipelines')
import gradeslib as G

d = json.load(gzip.open('/home/claude/nw/pipelines/cache/pagetext/csmb_b78.json.gz'))
PAGES = d['pages']

def collapsed(pdf):
    return re.sub(r'\s+', ' ', (PAGES[pdf - 1] or '').replace('\n', ' '))

def cut(pdf, start, end, drops=(), joins=()):
    t = collapsed(pdf)
    i = t.find(start)
    assert i >= 0, f'START not found pdf {pdf}: {start[:60]!r}'
    j = t.find(end, i)
    assert j >= 0, f'END not found pdf {pdf}: {end[:60]!r}'
    q = t[i:j + len(end)]
    for s in drops:
        assert s in q, f'DROP not in quote pdf {pdf}: {s[:50]!r}'
        q = q.replace(s, ' ')
    q = re.sub(r'\s+', ' ', q)
    for a, b in joins:
        assert a in q, f'JOIN not in quote pdf {pdf}: {a[:50]!r}'
        q = q.replace(a, b)
    return re.sub(r'\s+', ' ', q).strip()

R = []
def row(name, district, county, keys, anchor, pdf, printed, quote,
        hg=None, basis='production', years=None, tonnage=None, excl=None):
    R.append({
        'name': name, 'district': district, 'county': county, 'state': 'CA',
        'keys': keys, 'excl': excl or [], 'lat': None, 'lon': None,
        'anchor_hint': anchor, 'au_opt': None, 'ag_opt': None,
        'usd_per_ton': None, 'hg_flasks': hg, 'basis': basis,
        'years': years, 'price_year': None, 'tonnage': tonnage,
        'commodities': 'Mercury', 'src_key': 'csmb_b78',
        'page': printed, 'pdf_page': pdf, 'quote': quote})

# ---------------------------------------------------------------- LAKE ------
row('Abbott mine', 'Sulphur Creek (Lake)', 'Lake', ['abbott'],
    'Wilbur Springs, Colusa Co.', 79, 53,
    cut(79, 'The mine was discovered in 1862', '30,8-15 flasks.'),
    hg=30845, years='1870-1917', basis='production')

row('Great Western mine', 'Middletown (Lake)', 'Lake', ['great western'],
    'Middletown, Lake Co.', 84, 58,
    cut(84, 'It was opened up in 1873', '98,316 t^asks.'),
    hg=98316, years='1873-1909', basis='production')

row('Helen mine', 'Middletown (Lake)', 'Lake', ['helen'],
    'Middletown, Lake Co.', 85, 59,
    cut(85, 'The first recorded production of the Helen mine',
        '6.000 flasks to date.',
        drops=["(partly timbered) besides the mineral claims' area. "]),
    hg=6000, years='1873-1918', basis='production')

row('Helen mine (furnace-ore tenor)', 'Middletown (Lake)', 'Lake', ['helen'],
    'Middletown, Lake Co.', 86, 60,
    cut(86, 'The owner states that the ore being reduced',
        '0.25% of the metal.'),
    hg=None, years='1917', basis='production average')

row('Big Injun mine', 'Middletown (Lake)', 'Lake', ['big injun'],
    'Middletown, Lake Co.', 83, 57,
    cut(83, 'The ore retorted during 10 months', 'average of 2% mercury.',
        drops=['go to the table. ']),
    hg=None, years='1916-1917', basis='production average')

row('Mirabel mine (Bradford)', 'Middletown (Lake)', 'Lake',
    ['mirabel', 'bradford'], 'Middletown, Lake Co.', 88, 62,
    cut(88, 'The property is credited with a total yield', '30,600 iiasks.'),
    hg=30600, years='1887-1916', basis='production')

row('Sulphur Bank mine', 'Clear Lake (Lake)', 'Lake', ['sulphur bank'],
    'Clear Lake, 10 mi N of Lower Lake', 89, 63,
    cut(89, 'In 1899 the mine was reopened', 'approximately 92,400 flasks.',
        drops=['a steady ']),
    hg=92400, years='1873-1905', basis='production')

# ---------------------------------------------------------------- NAPA ------
row('Aetna Quicksilver mine', 'Pope Valley-Mayacmas (Napa)', 'Napa',
    ['aetna', 'tna'], 'Aetna Springs, Napa Co.', 105, 77,
    cut(105, 'The most important producing periods',
        '45,580 flasks to the end of 1917.',
        drops=['the Phoenix. ']),
    hg=45580, years='1877-1917', basis='production')

row('Corona mine', 'Oat Hill-Pope Valley (Napa)', 'Napa', ['corona'],
    '9 mi SE of Middletown, Napa Co.', 109, 81,
    cut(109, 'Figures of the total output of the Corona', '5000 flasks.',
        drops=['of 50 tons capacity. '],
        joins=[('approx- imatelv', 'approximatelv')]),
    hg=5000, years='1895-1906', basis='production')

row('Knoxville mine (Boston, Redington)', 'Knoxville (Napa)', 'Napa',
    ['knoxville', 'redington', 'boston'], 'Knoxville, Napa Co.', 113, 83,
    cut(113, 'The recorded total has', 'Xew Idria and Oat Ilill.',
        drops=['tion of 1908, the production has been small. ',
               'year) . '],
        joins=[]),
    hg=116204, years='1862-1917', basis='production')

row('Manhattan mine (Lake mine)', 'Knoxville (Napa)', 'Napa',
    ['manhattan'], 'Knoxville, Napa Co.', 116, 86,
    cut(116, 'It was idle from 1877 to 1884',
        '15,979 flasks of quicksilver.'),
    hg=15979, years='1862-1916', basis='production')

row('La Joya mine', 'Oakville (Napa)', 'Napa', ['la joya', 'joya'],
    '6 mi W of Oakville, Napa Co.', 116, 86,
    cut(116, 'From retort tests, the ore then being treated',
        'assayed 2% Hg.'),
    hg=None, years='1917', basis='assay')

row('Oat Hill mine (Napa Consolidated)', 'Oat Hill (Napa)', 'Napa',
    ['oat hill', 'napa consolidated'], '9 mi SE of Middletown, Napa Co.',
    118, 88,
    cut(118, 'This mine was for many years',
        '152,066 flasks from 1876 — to 1917 inclusive.'),
    hg=152066, years='1876-1917', basis='production')

row('Oat Hill mine (dump ore)', 'Oat Hill (Napa)', 'Napa',
    ['oat hill', 'napa consolidated'], '9 mi SE of Middletown, Napa Co.',
    119, 89,
    cut(119, 'Neweomb estimated that there are in excess',
        '0.15% quicksilver (3 pounds per ton).'),
    hg=None, years='1913-1917', basis='resource estimate',
    tonnage='250,000+ tons of dump ore')

row('Twin Peaks mine', 'Oat Hill (Napa)', 'Napa', ['twin peaks'],
    '9 mi NE of Calistoga, Napa Co.', 121, 91,
    cut(121, 'The mine is credited with a total',
        'the hanging wall, serpentine.',
        drops=['present owners, then under a lease. ']),
    hg=275, years='1904-1917', basis='production')

# --------------------------------------------------------- SANTA CLARA ------
row('New Almaden mine', 'New Almaden (Santa Clara)', 'Santa Clara',
    ['new almaden'], 'New Almaden, 12 mi S of San Jose', 209, 161,
    cut(209, 'The total production has been 1,021,183',
        '(Almaden Mine, Spain)'),
    hg=1021183, years='1845-1917', basis='production',
    excl=['spain'])

row('Guadalupe mine', 'New Almaden (Santa Clara)', 'Santa Clara',
    ['guadalupe'], '10 mi S of San Jose, Santa Clara Co.', 205, 159,
    cut(205, 'since which time it has been an important',
        '105.772 tiasks to the end of 1917.'),
    hg=105772, years='1850s-1917', basis='production')

# ----------------------------------------------------------- SAN BENITO -----
row('New Idria mine', 'New Idria (San Benito)', 'San Benito',
    ['new idria', 'idria'], 'Idria, San Benito Co.', 143, 109,
    cut(143, "97% of San Benito County's recorded production",
        'from 1858 to 1917 (inc.).',
        drops=['The mine has been ']),
    hg=306475, years='1858-1917', basis='production')

row('Cerro Bonito mine', 'Central San Benito (San Benito)', 'San Benito',
    ['cerro bonito', 'cerro benito'], '2 mi S of Llanada, San Benito Co.',
    136, 102,
    cut(136, 'A Knox and Osborne furnace', 'about 800 flasks.',
        drops=['The mine showed little activity after ']),
    hg=800, years='pre-1876', basis='production')

row('Hernandez mine (Los Picachos)', 'New Idria (San Benito)', 'San Benito',
    ['hernandez', 'picachos'], 'Hernandez, San Benito Co.', 141, 107,
    cut(141, 'The ore', '150 pounds of mercury per ton.',
        drops=['deposition was a layer of silica crystals of equal '
               'thickne.ss. ']),
    hg=None, years='1915', basis='ore shipped')

row('Stayton mine (group)', 'Stayton (San Benito)', 'San Benito',
    ['stayton'], '15 mi E of Hollister, San Benito Co.', 155, 121,
    cut(155, '800 to 1000 Hasks', "with a 'D' retort."),
    hg=800, years='pre-1880', basis='production',
    tonnage='4000 tons of ore stated on dump')

# --------------------------------------------------------- CONTRA COSTA -----
row('Ryne mine', 'Mount Diablo (Contra Costa)', 'Contra Costa',
    ['ryne', 'rhyne', 'mount diablo', 'mt. diablo'],
    'N peak of Mt. Diablo, Contra Costa Co.', 67, 41,
    cut(67, 'During the tine and black opal. late seventies the Ryne mine',
        '85 flasks per month, for a time.',
        drops=['tine and black opal. ']),
    hg=85, years='late 1870s (rate per month)', basis='production',
    tonnage='rate: up to 85 flasks per month')

# ---------------------------------------------------------------- COLUSA ----
row('Manzanita mine', 'Sulphur Creek (Colusa)', 'Colusa', ['manzanita'],
    'Wilbur Springs, Colusa Co.', 65, 39,
    cut(65, 'mine approximates 2,000 flasks',
        'eight years up to 1912.'),
    hg=2000, years='1904-1912', basis='production')

# -------------------------------------------------------------- DEL NORTE ---
row('Diamond Creek Cinnabar Co.', 'Diamond Creek (Del Norte)', 'Del Norte',
    ['diamond creek'], '18 mi from Monumental, Del Norte Co.', 67, 41,
    cut(67, 'The first ton of ore treated', 'l%-2% mercury.',
        drops=['Bibl. : ']),
    hg=None, years='1917', basis='value-text')

# ------------------------------------------------------------------ KERN ----
row('Cuddeback Cinnabar mine (chief producer of Kern Co. output)',
    'Tehachapi (Kern)', 'Kern', ['cuddeback'],
    'Tehachapi, Kern Co.', 73, 47,
    cut(73, 'Slightly over 300 flasks',
        'coming from the Cnddeback.'),
    hg=300, years='1916-1917', basis='production')

# -------------------------------------------------------------- MONTEREY ----
row('Patriquin Quicksilver mine', 'Parkfield (Monterey)', 'Monterey',
    ['patriquin'], 'Parkfield, Monterey Co.', 101, 73,
    cut(101, 'The mine has now been in steady operation',
        '511 flasks to the end of 1917.'),
    hg=511, years='1873-1917', basis='production')

row('Patriquin Quicksilver mine (retort-run tenor)', 'Parkfield (Monterey)',
    'Monterey', ['patriquin'], 'Parkfield, Monterey Co.', 103, 75,
    cut(103, 'The nearly 3 tons of ore treated', '2%-2.6% mercury.'),
    hg=None, years='1917', basis='production average')

# ------------------------------------------------------- SAN LUIS OBISPO ----
row('Klau mine', 'Adelaide (San Luis Obispo)', 'San Luis Obispo',
    ['klau'], '16 mi W of Paso Robles, San Luis Obispo Co.', 174, 136,
    cut(174, 'The recorded production of the Klau mine',
        '14.213 flasks, to the end of 1917.'),
    hg=14213, years='1874-1917', basis='production')

row('Oceanic mine', 'Oceanic-Cambria (San Luis Obispo)', 'San Luis Obispo',
    ['oceanic'], '5 mi E of Cambria, San Luis Obispo Co.', 180, 142,
    cut(180, 'the Oceanic iidnc to end of llic',
        '28.251 flasks of quick- silver.'),
    hg=28251, years='1876-1917', basis='production')

row('Deer Trail mine', 'Huasna (San Luis Obispo)', 'San Luis Obispo',
    ['deer trail'], '7 mi E of Huasna, San Luis Obispo Co.', 171, 133,
    cut(171, 'The Deer Trail group was located in 1915',
        '70 flasks of quicksilver.'),
    hg=70, years='1916 (3 months)', basis='production')

# ---------------------------------------------------------------- SONOMA ----
row('Cloverdale mine', 'Mayacmas (Sonoma)', 'Sonoma', ['cloverdale'],
    '12 mi E of Cloverdale, Sonoma Co.', 237, 183,
    cut(237, 'The total recorded Cloverdale Mine. production',
        "2200' 6738 fla.sks."),
    hg=6738, years='1872-1917', basis='production')

row('Buckeye claim (Mt. Vernon)', 'Mayacmas (Sonoma)', 'Sonoma',
    ['buckeye', 'mt. vernon'], '11 mi E of Cloverdale, Sonoma Co.', 237, 183,
    cut(237, 'There are 200 tons', 'broken in the open-cut.',
        drops=['There is ']),
    hg=None, years='1917-1918', basis='resource estimate',
    tonnage='200 tons broken in open-cut')

row('Culver-Baer mine', 'Mayacmas-Geysers (Sonoma)', 'Sonoma',
    ['culver-baer', 'culver'], '20 mi ESE of Cloverdale, Sonoma Co.',
    239, 185,
    cut(239, '1875 the Oakland mine', 'of 8922 flasks.',
        drops=['R. 9 W., 20 miles south of east ']),
    hg=8922, years='1870s-1917', basis='production')

row('Great Eastern mine (incl. Mt. Jackson)', 'Guerneville (Sonoma)',
    'Sonoma', ['great eastern', 'mt. jackson', 'mount jackson'],
    '4 mi NE of Guerneville, Sonoma Co.', 241, 187,
    cut(241, 'Production of', 'Sonoma County to the end of 1917.',
        drops=['Great Eastern Mine. ']),
    hg=42092, years='1875-1917', basis='production')

row('Socrates mine', 'Pine Flat (Sonoma)', 'Sonoma', ['socrates'],
    'Pine Flat, 6 mi SE of The Geysers', 247, 193,
    cut(247, 'The recorded production for the years 1900-1917',
        'about 1000 flasks.'),
    hg=3500, years='1900-1917', basis='production')

row('Rattlesnake mine', 'Pine Flat (Sonoma)', 'Sonoma', ['rattlesnake'],
    'Pine Flat, Sonoma Co.', 246, 192,
    cut(246, 'This mine is credited with a production of 65 flasks',
        'an oily bitumen.'),
    hg=65, years='1875', basis='production')

# --------------------------------------------------------------- TRINITY ----
row('Altoona mine', 'Altoona-Carrville (Trinity)', 'Trinity',
    ['altoona'], '15 mi NE of Carrville, Trinity Co.', 255, 201,
    cut(255, 'The total production to date of the Altoona',
        'contradictory in this respect.',
        drops=['eatiou. '],
        joins=[('rec- ords', 'records')]),
    hg=29000, years='1875-1911', basis='production')

# ------------------------------------------------------------- STANISLAUS ---
row('Phoenix mines (Summit, Grayson, Orestimba)', 'Orestimba (Stanislaus)',
    'Stanislaus', ['phoenix', 'orestimba'],
    '24 mi SW of Patterson, Stanislaus Co.', 252, 198,
    cut(252, 'The total production to date lias been', 'nearly 200 fla.sks.',
        drops=[]),
    hg=200, years='1870s-1916', basis='production', excl=['aetna'])

# ------------------------------------------------------------------ YOLO ----
row('Reed mine (California)', 'Knoxville district (Yolo)', 'Yolo',
    ['reed'], 'SW of Rumsey, Yolo Co.', 259, 205,
    cut(259, 'The J. B. Randol table', '5,653 fla.sks between 1876 and 1880.'),
    hg=5653, years='1876-1880', basis='production')

# ------------------------------------------------------------- score+dump ---
out = '/home/claude/nw/grades-research/agent_out/rows_csmb_b78.json'
bad = 0
for r in R:
    s = G.quote_on_page(r['quote'], PAGES[r['pdf_page'] - 1])
    flag = '' if s >= 0.9 else ('  <-- LOW' if s >= 0.85 else '  <-- FAIL')
    if s < 0.9:
        bad += 1
    print(f"{s:0.3f}  {r['name'][:44]:44s} p.{r['page']}/pdf{r['pdf_page']}{flag}")
json.dump(R, open(out, 'w'), indent=1)
print(f'\n{len(R)} rows written -> {out}; {bad} below 0.90')
