#!/usr/bin/env python3
"""Build rows_b528_pp97.json: extract verbatim quotes from cached pagetext
by start/end anchors so every quote validates exactly."""
import json, re, sys, os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                '..', '..', 'pipelines'))
import gradeslib as G

PT = {k: G.load_pagetext(k) for k in ('b528', 'pp97')}

def _anchor_re(a):
    """Anchor -> regex tolerant of any whitespace/line breaks."""
    parts = re.split(r'\s+', a.strip())
    return re.compile(r'\s+'.join(re.escape(p) for p in parts))

def pull(src, pdf_page, start, end):
    """Verbatim span from page text, hyphen-line-joined + ws-collapsed."""
    tx = PT[src]['pages'][pdf_page - 1]
    ms = _anchor_re(start).search(tx)
    assert ms, f'{src} p{pdf_page}: start anchor not found: {start[:40]!r}'
    me = _anchor_re(end).search(tx, ms.start())
    assert me, f'{src} p{pdf_page}: end anchor not found: {end[:40]!r}'
    raw = tx[ms.start():me.end()]
    q = re.sub(r'-\s*\n\s*', '', raw)      # join hyphenated line breaks
    q = re.sub(r'\s+', ' ', q).strip()     # collapse whitespace
    return q

R = []
def row(name, district, county, keys, anchor, page, pdf_page, start, end,
        src_key, basis, years, price_year, commodities, metal='Au',
        excl=None, tonnage=None, literal=None, **fields):
    r = {
        'name': name, 'district': district, 'county': county, 'state': 'ID',
        'keys': keys, 'excl': excl or [], 'lat': None, 'lon': None,
        'anchor_hint': anchor, 'metal': metal,
        'au_opt': None, 'ag_opt': None, 'au_gpt': None, 'usd_per_ton': None,
        'pb_pct': None, 'zn_pct': None, 'cu_pct': None, 'sb_pct': None,
        'wo3_units': None, 'hg_flasks': None, 'usd_per_yd3': None, 'plc': None,
        'basis': basis, 'years': years, 'price_year': price_year,
        'tonnage': tonnage, 'commodities': commodities,
        'src_key': src_key, 'page': page, 'pdf_page': pdf_page,
        'quote': literal if literal is not None
                 else pull(src_key, pdf_page, start, end),
    }
    if literal is not None:
        s = G.quote_on_page(literal, PT[src_key]['pages'][pdf_page - 1])
        assert s >= 0.85, f'{name}: literal quote scores {s:.3f}'
    r.update(fields)
    R.append(r)

# ----------------------------------------------------------- b528 (Lemhi) --
row('Viola mine (early shipments)', 'Nicholia (Lemhi)', 'Lemhi',
    ['viola'], 'Nicholia, Lemhi Co.', 84, 96,
    'It is said that 5,000 to 7,000 tons',
    'were\nthus transported.',
    'b528', 'ore shipped', '1882-1885', 1884, 'Lead, Silver',
    tonnage='5,000-7,000 tons hauled to Camas for shipment, 1882-1885',
    pb_pct=50.0, ag_opt=10.0)

row('Viola mine (lead carbonate ore)', 'Nicholia (Lemhi)', 'Lemhi',
    ['viola'], 'Nicholia, Lemhi Co.', 84, 96,
    'The ore was le.ad carbonate',
    'silver per ton.',
    'b528', 'production', '1882-1890', 1886, 'Lead, Silver',
    pb_pct=35.0, ag_opt=4.0)

row('Lemhi Union mine (57-ton lot)', 'Spring Mountain (Lemhi)', 'Lemhi',
    ['lemhi union'], 'Hahn, Lemhi Co.', 88, 100,
    'Analyses of two lots of ore handled',
    '4.3 per cent calcium oxide.',
    'b528', 'ore shipped', 'pre-1913', 1911, 'Lead, Silver',
    tonnage='57-ton smelter lot',
    pb_pct=39.0, ag_opt=12.0)

row('Elizabeth and Teddy mines', 'Spring Mountain (Lemhi)', 'Lemhi',
    ['elizabeth', 'teddy'], 'Hahn, Lemhi Co.', 89, 101,
    'Together, the properties furnished the Hahn smelter',
    '10 per cent calcium\noxide.',
    'b528', 'production average', 'pre-1913', 1911, 'Lead, Silver',
    tonnage='400 tons furnished to the Hahn smelter',
    pb_pct=20.0, ag_opt=11.0)

row('Pittsburgh-Idaho mine', 'Texas-Gilmore (Lemhi)', 'Lemhi',
    ['pittsburg'], 'Gilmore, Lemhi Co.', 103, 116,
    'The ore averages about 37 per cent',
    'has\nbeen noted in its tenor.',
    'b528', 'production average', 'pre-1913', 1912, 'Lead, Silver',
    tonnage='about 12,000 tons of lead bullion and 500,000 oz silver to Sept. 1911',
    pb_pct=37.0, ag_opt=15.25)

row('Latest Out mine', 'Texas-Gilmore (Lemhi)', 'Lemhi',
    ['latest out'], 'Gilmore, Lemhi Co.', 107, 120,
    'In general, the ore runs about 18 ounces',
    '5 per cent zinc.',
    'b528', 'production average', 'pre-1913', 1912, 'Lead, Silver',
    tonnage='gross production about $350,000 to September 1911',
    ag_opt=18.0, pb_pct=34.0, zn_pct=5.0)

row('Martha-Dorothy gold vein (Allie Mining Co.)', 'Texas-Gilmore (Lemhi)',
    'Lemhi', ['martha', 'allie'], 'Gilmore, Lemhi Co.', 107, 120,
    'Recently, however,\na promising gold vein',
    'are blocked out.',
    'b528', 'resource estimate', '1912', 1912, 'Gold',
    tonnage='about 15,000 tons blocked out',
    usd_per_ton=12.0)

row('Jumbo mine (Ulich Gulch)', 'Texas-Gilmore (Lemhi)', 'Lemhi',
    ['jumbo'], 'Gilmore, Lemhi Co.', 108, 121,
    'About 400\ntons of ore running',
    'shipped from the property.',
    'b528', 'ore shipped', 'pre-1913', 1912, 'Lead, Silver, Gold',
    tonnage='about 400 tons shipped',
    pb_pct=37.0, ag_opt=48.0, usd_per_ton=3.50)

row('Democrat mine', 'Texas-Gilmore (Lemhi)', 'Lemhi',
    ['democrat'], 'Gilmore, Lemhi Co.', 109, 122,
    'The gangue is very siliceous',
    '4 ounces in silver.',
    'b528', 'value-text', 'pre-1913', 1912, 'Lead, Silver',
    pb_pct=9.0, ag_opt=4.0)

row('Leadville mine (Junction district, better-grade ore)',
    'Junction (Lemhi)', 'Lemhi',
    ['leadville'], 'Junction (Leadore), Lemhi Co.', 114, 127,
    'In this property the predominating ore',
    '35 ounces of silver per ton.',
    'b528', 'value-text', 'pre-1913', 1912, 'Lead, Silver',
    pb_pct=50.0, ag_opt=28.0)

row('Copper Queen mine (18-car shipments)', 'McDevitt (Lemhi)', 'Lemhi',
    ['copper queen'], 'Agency Creek, McDevitt, Lemhi Co.', 120, 134,
    'The production consists of 480 ounces',
    '$24.75 in gold to the ton.',
    'b528', 'ore shipped', '1905-1912', 1910, 'Copper, Gold, Silver',
    tonnage='returns from 18 cars',
    cu_pct=28.3, ag_opt=6.0, usd_per_ton=24.75)

row('Gibbonsville district (milled ore)', 'Gibbonsville (Lemhi)', 'Lemhi',
    ['gibbonsville'], 'Gibbonsville, Lemhi Co.', 128, 142,
    'Much of the ore milled has contained',
    '70 per cent has been recovered.',
    'b528', 'district production', '1877-1912', 1900, 'Gold',
    tonnage='district production estimated at $2,000,000',
    usd_per_ton=20.0)

row('Ulysses mine (workable ore bodies)', 'Indian Creek (Lemhi)', 'Lemhi',
    ['ulysses'], 'Ulysses, Lemhi Co.', 137, 154,
    'Gold is\ndistributed quite generally',
    'average $7 or $8 per ton.',
    'b528', 'value-text', 'pre-1913', 1912, 'Gold',
    usd_per_ton=7.0)

row('Kentuck mine', 'Mineral Hill (Lemhi)', 'Lemhi',
    ['kentuck'], 'Shoup, Lemhi Co.', 143, 161,
    'yielded about 45,000 tons of ore',
    'less than $10 a ton.',
    'b528', 'production average', '1884-1893', 1890, 'Gold',
    tonnage='about 45,000 tons of ore',
    usd_per_ton=10.0)

row('Monolith mine', 'Mineral Hill (Lemhi)', 'Lemhi',
    ['monolith'], 'Shoup, Lemhi Co.', 143, 161,
    'The\nproduction of the property is said to be about $175,000',
    'less than $10 a ton.',
    'b528', 'production average', 'pre-1913', 1910, 'Gold',
    tonnage='production about $175,000',
    usd_per_ton=10.0)

row('Clipper Bullion mine (rich shoot)', 'Mineral Hill (Lemhi)', 'Lemhi',
    ['clipper bullion'], 'Shoup, Lemhi Co.', 145, 163,
    'the ore occurs in small shoots',
    'averaging $43 per ton.',
    'b528', 'production', '1887-1912', 1900, 'Gold',
    tonnage='one shoot produced 1,000 tons',
    usd_per_ton=43.0)

row('Italian mine (1910 trial run)', 'Mackinaw (Lemhi)', 'Lemhi',
    ['italian'], 'Leesburg, Lemhi Co.', 153, 172,
    'A trial run of 4,000 tons',
    '87 cents per ton.',
    'b528', 'production', '1910', 1910, 'Gold',
    tonnage='trial run of 4,000 tons',
    usd_per_ton=2.25)

row('Haidee mine', 'Mackinaw (Lemhi)', 'Lemhi',
    ['haidee'], 'Leesburg, Lemhi Co.', 154, 173,
    'The ore is said to have plated',
    'per ton in gold.',
    'b528', 'value-text', 'pre-1913', 1912, 'Gold',
    usd_per_ton=7.0)

row('Gold Flint mine', 'Mackinaw (Lemhi)', 'Lemhi',
    ['gold flint'], 'Leesburg, Lemhi Co.', 154, 173,
    'Gold, accompanied by some silver, is claimed',
    'about $5 per ton.',
    'b528', 'value-text', 'pre-1913', 1912, 'Gold, Silver',
    usd_per_ton=5.0)

row('Queen of the Hills mine (Eva shoot)', 'Eureka (Lemhi)', 'Lemhi',
    ['queen of the hills'], 'Salmon, Lemhi Co.', 158, 177,
    'The\naverage gold content for the Eva shoot',
    'it was $19.40 per ton.',
    'b528', 'value-text', 'pre-1913', 1912, 'Gold',
    usd_per_ton=3.50)

row('U. P. & Burlington mine (last mill run)', 'Eureka (Lemhi)', 'Lemhi',
    ['burlington'], 'Salmon, Lemhi Co.', 158, 177,
    'The total production is perhaps',
    'last mill\nrun of 40 tons.',
    'b528', 'production', 'pre-1913', 1912, 'Gold',
    tonnage='mill run of 40 tons yielding $800 ($20/ton derived)',
    usd_per_ton=20.0)

row('Moose Creek veins (principal shoot)', 'Blackbird (Lemhi)', 'Lemhi',
    ['moose creek'], 'Blackbird district, Lemhi Co.', 162, 182,
    'It is noteworthy that of the several veins',
    'averages about $20 a ton\nin gold.',
    'b528', 'value-text', 'pre-1913', 1912, 'Gold',
    excl=['placer'],
    usd_per_ton=20.0)

row('Blackbird mines (Brown Bear workings)', 'Blackbird (Lemhi)', 'Lemhi',
    ['blackbird', 'brown bear'], 'Blackbird (Cobalt), Lemhi Co.', 164, 184,
    'The ore is sought for\ncopper and gold',
    '$1.50 in gold to the ton.',
    'b528', 'value-text', 'pre-1913', 1907, 'Copper, Gold, Cobalt',
    cu_pct=3.0, usd_per_ton=1.50)

row('Musgrove group (better-grade ore)', 'Blackbird (Lemhi)', 'Lemhi',
    ['musgrove'], 'Musgrove Creek, Blackbird district, Lemhi Co.', 165, 185,
    'It is such material which constitutes',
    'running as high as $75.',
    'b528', 'value-text', 'pre-1913', 1912, 'Gold',
    usd_per_ton=20.0)

row('Yellow Jacket mine', 'Yellow Jacket (Lemhi)', 'Lemhi',
    ['yellow jacket', 'yellowjacket'], 'Yellowjacket, Lemhi Co.', 166, 186,
    'The production of the camp has come largely',
    'total content of the ore.',
    'b528', 'production average', '1882-1897', 1890, 'Gold',
    tonnage='total yield about $450,000',
    usd_per_ton=5.50)

row('Monument mine (Myers Cove)', 'Gravel Range (Lemhi)', 'Lemhi',
    ['monument'], 'Myers Cove, Lemhi Co.', 175, 195,
    'Assays have been secured\nfrom the Monument mine',
    'said to\nbe about $11.',
    'b528', 'value-text', 'pre-1913', 1912, 'Gold, Silver',
    usd_per_ton=11.0)

# ---------------------------------------------------------- pp97 (Mackay) --
row('Empire mine (1913 company and lessee shipments)',
    'Alder Creek-Mackay (Custer)', 'Custer',
    ['empire'], 'Mackay, Custer Co.', 94, 112,
    None, None,
    'pp97', 'ore shipped', '1913', 1913, 'Copper, Gold, Silver',
    tonnage='34,721.69 dry tons shipped in 1913',
    literal=('In 1913 the company and lessees shipped 34,721.69 dry tons '
             'of ore having an average assay value of 5.37 per cent copper '
             'and 0.052 ounce gold and 2.968 ounces silver to the ton.'),
    cu_pct=5.37, au_opt=0.052, ag_opt=2.968)

row('Empire mine (Copper Bullion sulphide ore, eight 50-ton lots)',
    'Alder Creek-Mackay (Custer)', 'Custer',
    ['empire', 'copper bullion'], 'Mackay, Custer Co.', 98, 116,
    'Analyses of the Alberta sulphide ores',
    'ounces of silver to the ton.',
    'pp97', 'ore shipped', '1912-1913', 1913, 'Copper, Gold, Silver',
    tonnage='average of eight 50-ton smelter lots',
    excl=['clipper'],
    cu_pct=5.73, au_opt=0.073, ag_opt=2.18)

row('Grand Prize mine', 'Alder Creek-Mackay (Custer)', 'Custer',
    ['grand prize'], 'Mackay, Custer Co.', 101, 119,
    'During the sinking of the shaft lead',
    'returns of about $10,000.',
    'pp97', 'production', 'pre-1914', 1912, 'Lead, Silver',
    ag_opt=8.0)

row('Champion group (tunnel No. 1 shipment)',
    'Alder Creek-Mackay (Custer)', 'Custer',
    ['champion'], 'Mackay, Custer Co.', 101, 119,
    'A }ecent shipment from this level',
    'ounces of silver to the ton.',
    'pp97', 'ore shipped', '1912-1913', 1913, 'Lead, Silver, Zinc',
    pb_pct=20.3, zn_pct=5.0, ag_opt=6.0)

row('Easlie group (1909 car of ore)', 'Alder Creek-Mackay (Custer)', 'Custer',
    ['easlie'], 'Mackay, Custer Co.', 103, 121,
    'A small car\nof ore containing',
    'of the limestone in 1909.',
    'pp97', 'ore shipped', '1909', 1909, 'Lead, Silver',
    tonnage='a small car of ore',
    pb_pct=30.0, ag_opt=8.0)

row('Copper Basin district (oxidized copper ores)',
    'Copper Basin (Custer)', 'Custer',
    ['copper basin'], 'Copper Basin, Custer Co.', 103, 121,
    'Most of the copper production',
    '$3 in gold to the ton.',
    'pp97', 'district production', '1888-1912', 1905, 'Copper, Silver, Gold',
    tonnage='district production about $100,000',
    cu_pct=5.0, ag_opt=10.0, usd_per_ton=3.0)

row('Muldoon mine (first ore, upper workings)', 'Muldoon (Blaine)', 'Blaine',
    ['muldoon'], 'Muldoon, Blaine Co.', 109, 127,
    'It was reported, however,',
    'ounces of silver to the ton.',
    'pp97', 'value-text', '1881-1885', 1883, 'Lead, Silver',
    metal='Ag', ag_opt=60.0)

row('Muldoon mine (level No. 5 ore)', 'Muldoon (Blaine)', 'Blaine',
    ['muldoon'], 'Muldoon, Blaine Co.', 110, 128,
    'This level, which was caved',
    'ounces of silver to\nthe ton.',
    'pp97', 'production', '1881-1890', 1885, 'Lead, Silver',
    pb_pct=50.0, ag_opt=45.0)

row('Kaufman & Weaver claims (80-foot shaft vein)',
    'Skull Canyon (Lemhi)', 'Lemhi',
    ['kaufman'], 'Kaufman, Birch Creek, Lemhi Co.', 83, 99,
    'One of these\nveins is about 4 fe et wide',
    'ounces .of silver to the ton.',
    'pp97', 'value-text', 'pre-1914', 1912, 'Lead, Silver',
    pb_pct=20.0, ag_opt=2.0)

out = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                   'rows_b528_pp97.json')
json.dump(R, open(out, 'w'), indent=1)
print(f'wrote {len(R)} rows -> {out}')
