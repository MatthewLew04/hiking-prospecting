#!/usr/bin/env python3
"""Build rows_ismir_r2.json — WS9 round-2 deeper cuts from ismir1915/1917/1918."""
import json, gzip, re, os

PT = '/home/claude/nw/pipelines/cache/pagetext'
data = {k: json.load(gzip.open(os.path.join(PT, k + '.json.gz'), 'rt'))
        for k in ['ismir1915', 'ismir1917', 'ismir1918']}

def snip(key, pdf, start, end):
    pg = data[key]['pages'][pdf - 1]
    i = pg.find(start)
    assert i >= 0, f'START not found: {key} p{pdf} :: {start[:50]!r}'
    j = pg.find(end, i)
    assert j >= 0, f'END not found: {key} p{pdf} :: {end[:50]!r}'
    t = pg[i:j + len(end)]
    t = re.sub(r'-\s*\n\s*', '', t)      # join hyphenated line breaks
    t = re.sub(r'\s+', ' ', t).strip()   # collapse whitespace
    return t

R = []
def row(name, district, county, keys, anchor, src, page, pdf, start, end,
        excl=None, metal='Au', **kw):
    r = {'name': name, 'district': district, 'county': county, 'state': 'ID',
         'keys': keys, 'excl': excl or [], 'lat': None, 'lon': None,
         'anchor_hint': anchor, 'metal': metal,
         'au_opt': None, 'ag_opt': None, 'au_gpt': None, 'usd_per_ton': None,
         'pb_pct': None, 'zn_pct': None, 'cu_pct': None, 'sb_pct': None,
         'wo3_units': None, 'hg_flasks': None, 'usd_per_yd3': None, 'plc': None,
         'basis': kw.pop('basis'), 'years': kw.pop('years'),
         'price_year': kw.pop('price_year'), 'tonnage': kw.pop('tonnage', None),
         'commodities': kw.pop('commodities'),
         'src_key': src, 'page': page, 'pdf_page': pdf,
         'quote': snip(src, pdf, start, end)}
    r.update(kw)
    R.append(r)

# ============================================================= ismir1915 ===
# --- Seven Devils (Adams Co.), pdf 99 = printed 96
row('Peacock mine (shipping record)', 'Seven Devils (Adams)', 'Adams',
    ['peacock'], 'Landore, Adams Co.', 'ismir1915', 96, 99,
    'old Peacock', 'grano-diorite that', cu_pct=10.0,
    basis='ore shipped', years='pre-1915', price_year=1915,
    tonnage='20,000 tons shipped', commodities='Copper')
row('Peacock mine (engineer\'s resource estimate)', 'Seven Devils (Adams)',
    'Adams', ['peacock'], 'Landore, Adams Co.', 'ismir1915', 96, 99,
    'whose shallow shaft', 'green carhonate.', cu_pct=6.0,
    basis='resource estimate', years='1915', price_year=1915,
    tonnage='50,000 tons resource', commodities='Copper')
row('Blue Jacket-Queen group (hand-picked shipments)', 'Seven Devils (Adams)',
    'Adams', ['blue jacket'], 'Landore, Adams Co.', 'ismir1915', 96, 99,
    'one of which, the Blne', 'authentic recordsi.', cu_pct=30.0,
    basis='ore shipped', years='pre-1915', price_year=1915,
    tonnage='50-60 carloads hand-picked', commodities='Copper')
# --- Coeur d'Alene high-grade lead shipments, pdf 44 = printed 41
row('Callahan vein (Interstate-Callahan mine)',
    'Nine Mile, Coeur d\'Alene (Shoshone)', 'Shoshone',
    ['callahan'], 'Wallace, Shoshone Co.', 'ismir1915', 41, 44,
    'and one of these, the Callahan', 'averaging\n80 per cent lead.', pb_pct=70.0,
    basis='ore shipped', years='pre-1915', price_year=1915,
    tonnage='60 carloads (record output)', commodities='Lead, silver')
row('Hypotheek mine (900-foot level carload)', 'Coeur d\'Alene (Shoshone)',
    'Shoshone', ['hypotheek'], 'Kellogg, Shoshone Co.', 'ismir1915', 41, 44,
    'at the Hypotheek', '68 per cent lead and no\nzinc', pb_pct=68.0,
    basis='ore shipped', years='1915', price_year=1915,
    tonnage='one carload hand-sorted', commodities='Lead')
row('Highland Surprise mine', 'Pine Creek, Coeur d\'Alene (Shoshone)',
    'Shoshone', ['highland surprise', 'highland-surprise'],
    'Kellogg, Shoshone Co.', 'ismir1915', 41, 44,
    'several car loads of ore were shipped', 'Prichard form,ation.',
    pb_pct=66.0, basis='ore shipped', years='1915', price_year=1915,
    tonnage='several carloads', commodities='Lead')
row('Little Pittsburg mine (new zinc body)',
    'Pine Creek, Coeur d\'Alene (Shoshone)', 'Shoshone',
    ['little pittsburg'], 'Kellogg, Shoshone Co.', 'ismir1915', 63, 66,
    'the new. zinc discoveries at the', 'average 30 per cent zinc.',
    zn_pct=30.0, basis='value-text', years='1915', price_year=1915,
    commodities='Zinc')
row('Horse Powell mine', 'Little North Fork, Coeur d\'Alene (Shoshone)',
    'Shoshone', ['horse powell'], 'Kellogg, Shoshone Co.', 'ismir1915', 65, 68,
    'on the the Horse', 'gold and\n~.;ilver per ton.', cu_pct=3.0,
    basis='resource estimate', years='1915', price_year=1915,
    tonnage='50,000 tons developed', commodities='Copper, gold, silver')
# --- 1915 antimony cluster
row('Stanley mine antimony vein (Burke)', 'Burke, Coeur d\'Alene (Shoshone)',
    'Shoshone', ['stanley'], 'Burke, Shoshone Co.', 'ismir1915', 123, 126,
    'carrying practically nothing but\nstibnite', 'high grade product.',
    sb_pct=10.0, basis='assay', years='1915', price_year=1915,
    commodities='Antimony, gold')
row('Coeur d\'Alene Company antimony mine (Pine Creek dump shipments)',
    'Pine Creek, Coeur d\'Alene (Shoshone)', 'Shoshone',
    ['coeur d\'alene antimony'], 'Kellogg, Shoshone Co.',
    'ismir1915', 124, 127,
    'Tlhis company shipped 19 tons', 'residues of the mine.', sb_pct=34.0,
    basis='ore shipped', years='1915', price_year=1915,
    tonnage='19 tons of 34% + 22 tons of 37%, hand-sorted dump residues',
    commodities='Antimony')
row('Star Antimony mine (Stewart Creek)',
    'Pine Creek, Coeur d\'Alene (Shoshone)', 'Shoshone',
    ['star antimony'], 'Kellogg, Shoshone Co.', 'ismir1915', 124, 127,
    'the Star Antimony', 'trace of\narsenic.', sb_pct=60.0,
    basis='production', years='1915', price_year=1915,
    tonnage='1.5 tons per day hand-sorted', commodities='Antimony')
row('Pearson Antimony vein (west fork Pine Creek)',
    'Pine Creek, Coeur d\'Alene (Shoshone)', 'Shoshone',
    ['pearson'], 'Kellogg, Shoshone Co.', 'ismir1915', 124, 127,
    'On the west fork', '58\nper cent antimony sulphide.', sb_pct=35.0,
    basis='ore shipped', years='1915', price_year=1915,
    tonnage='14 tons of 35% + 6 tons of 58%', commodities='Antimony')
# --- Ima tungsten, pdf 123 = printed 120
row('Ima mine (tungsten concentrates shipped)', 'Patterson (Lemhi)', 'Lemhi',
    ['ima'], 'Patterson, Lemhi Co.', 'ismir1915', 120, 123,
    'lllaking an intertesting production', '60 per cent\ntnngstic acid.',
    wo3_units=60.0, basis='ore shipped', years='1915', price_year=1915,
    tonnage='about 12 tons of concentrates', commodities='Tungsten')
# --- Indian Creek, Snake River canyon (Adams Co.), pdf 102 = printed 99
row('Indian Creek lead-zinc vein (Snake River canyon)',
    'Snake River canyon (Adams)', 'Adams', ['indian creek'],
    'Homestead, Ore. (Idaho side), Adams Co.', 'ismir1915', 99, 102,
    'One vein in this locality', 'ounces silver', pb_pct=30.0, zn_pct=30.0,
    ag_opt=20.0, basis='assay-text', years='1915', price_year=1915,
    tonnage='printed page verified: 30% lead and 30% zinc (OCR: ao)',
    commodities='Lead, zinc, silver')
# --- Vienna district (Blaine Co.), pdf 60 = printed 57
row('Vienna district mines (Sawtooth range)', 'Vienna, Sawtooth (Blaine)',
    'Blaine', ['vienna'], 'Vienna, Blaine Co.', 'ismir1915', 57, 60,
    'In the deeper granite canyons', 'ore la tcly',
    metal='Ag', usd_per_ton=50.0, basis='ore shipped', years='1915',
    price_year=1915, tonnage='several carloads', commodities='Silver, lead, gold')

# ============================================================= ismir1917 ===
row('Keenan lease (adjoining Empire Copper, Mackay)', 'Mackay (Custer)',
    'Custer', ['keenan'], 'Mackay, Custer Co.', 'ismir1917', 35, 38,
    'During 1917 this operator', 'brown iron oxide.', pb_pct=6.0,
    basis='ore shipped', years='1917', price_year=1917,
    tonnage='10,000 tons in 1917', commodities='Lead, silver')
row('Big Hole Creek lead prospect (South Fork Payette)',
    'Big Hole Creek (Boise)', 'Boise', ['big hole'],
    'South Fork Payette River, Boise Co.', 'ismir1917', 51, 54,
    'A short cross-cut tunnel driven',
    'per\ncent zinc.', pb_pct=3.0, zn_pct=3.0, basis='assay',
    years='1917', price_year=1917, commodities='Lead, zinc')
row('Emmons zinc prospects (Lake Creek)', 'Wood River (Blaine)', 'Blaine',
    ['lake creek'], 'Ketchum, Blaine Co.', 'ismir1917', 61, 64,
    'About 15 miles north of the North Star', '50 per cent zinc.',
    zn_pct=37.0, basis='ore shipped', years='1917', price_year=1917,
    tonnage='six cars crude ore', commodities='Zinc')
row('Antelope Creek lead deposits', 'Antelope Creek (Butte)', 'Butte',
    ['antelope'], 'Arco, Butte Co.', 'ismir1917', 38, 41,
    'Some interesting development was made on Antelope',
    'high silver values.', pb_pct=12.0, basis='ore shipped', years='1917',
    price_year=1917, tonnage='a number of carloads', commodities='Lead, silver')
row('Teddy Claim and Excelsior Group (Spring Mountain direction)',
    'Gilmore (Lemhi)', 'Lemhi', ['teddy', 'excelsior'], 'Gilmore, Lemhi Co.',
    'ismir1917', 44, 47, 'In this\ndirection half a dozen',
    'silver\nto each unit.', pb_pct=18.0, basis='ore shipped', years='1917',
    price_year=1917, tonnage='12 carloads combined', commodities='Lead, silver')
row('Viola mine (original ore body)', 'Nicholia, Birch Creek (Lemhi)', 'Lemhi',
    ['viola'], 'Nicholia, Lemhi Co.', 'ismir1917', 45, 48,
    'It was extensively operated', 'production of\nbullion.', pb_pct=60.0,
    basis='ore shipped', years='1880s', price_year=1917,
    tonnage='60,000 tons when first opened (printed text verified: "60 per cent")',
    commodities='Lead')
row('Riverview mine (dump residue shipments)', 'Bay Horse (Custer)', 'Custer',
    ['riverview'], 'Bayhorse, Custer Co.', 'ismir1917', 47, 50,
    'Considerable shipments were made from\nquite large dump',
    'each unit of\nlead.', pb_pct=15.0, zn_pct=15.0, basis='ore shipped',
    years='1917', price_year=1917, tonnage='dump residues of former years',
    commodities='Lead, zinc, silver')
row('Livingstone Group (lenzy ore shoot)', 'Clayton, Salmon River (Custer)',
    'Custer', ['livingston'], 'Clayton, Custer Co.', 'ismir1917', 49, 52,
    'Another more lenzy ore shoot', '110 ounces\nsilver per ton.',
    pb_pct=20.0, ag_opt=110.0, basis='assay-text', years='1917',
    price_year=1917, commodities='Lead, silver')
row('Harmony mine (Anderson Group, Worthington Creek)',
    'Salmon City (Lemhi)', 'Lemhi', ['harmony', 'anderson'],
    'Salmon, Lemhi Co.', 'ismir1917', 71, 74,
    'point on this zone near the head of Worthington',
    'gold and silver per\nton.', cu_pct=6.0, basis='ore shipped',
    years='1917', price_year=1917, tonnage='10 carloads in four months',
    commodities='Copper, gold, silver')
row('Copper Basin Mining Co. (glory hole shipments)', 'Copper Basin (Custer)',
    'Custer', ['copper basin'], 'Mackay, Custer Co.', 'ismir1917', 69, 72,
    'a glory hole quarry has been opened', 'magnetic\niron.', cu_pct=5.0,
    basis='ore shipped', years='pre-1917', price_year=1917,
    tonnage='several thousand tons shipped crude', commodities='Copper')
row('Mizpah mine (Hoodoo district)', 'Hoodoo (Latah)', 'Latah',
    ['mizpah'], 'Harvard, Latah Co.', 'ismir1917', 77, 80,
    'shipped nearly 200 tons',
    'permanency at this time.', cu_pct=17.0, basis='ore shipped',
    years='1917', price_year=1917, tonnage='nearly 200 tons crude',
    commodities='Copper')
row('Richmond mine (St. Joe slope)', 'East Coeur d\'Alene, St. Joe (Shoshone)',
    'Shoshone', ['richmond'], 'Mullan, Shoshone Co.', 'ismir1917', 77, 80,
    'It employed a force of 40', '$3.00 per\nton gold.', cu_pct=6.0,
    usd_per_ton=3.0, basis='ore shipped', years='1917', price_year=1917,
    tonnage='a carload a day for several months', commodities='Copper, gold')
row('Fern Quicksilver mine (first Idaho quicksilver production)',
    'Yellow Pine-Monumental Creek (Idaho)', 'Idaho', ['fern'],
    'Yellow Pine, Idaho Co.', 'ismir1917', 95, 98,
    'For the first time in the', 'Monumental Creek\nSummit.',
    hg_flasks=5.0, basis='production', years='1917', price_year=1917,
    tonnage='5-ton test run', commodities='Mercury')
row('Yellow Pine Basin antimony vein', 'Yellow Pine (Valley)', 'Valley',
    ['yellow pine'], 'Yellow Pine, Valley Co.', 'ismir1917', 96, 99,
    'In the same region, at Yellow Pine Basin',
    '50 per cent ore bands.', sb_pct=50.0, basis='value-text', years='1917',
    price_year=1917, commodities='Antimony')

# ============================================================= ismir1918 ===
row('Independence mine (crude ore and concentrates)', 'Wood River (Blaine)',
    'Blaine', ['independence'], 'Ketchum, Blaine Co.', 'ismir1918', 61, 66,
    'FrOlll this work', 'eaeh unit of lead.', pb_pct=30.0,
    basis='production', years='1918', price_year=1918,
    tonnage='25 tons daily, crude and concentrates', commodities='Lead, silver')
row('Azurite mine (Snake River canyon zinc vein)',
    'Snake River canyon (Adams)', 'Adams', ['azurite'],
    'Ballard\'s Landing, Adams Co.', 'ismir1918', 73, 78,
    'An interesting deposit of clean, grey zinc', 'grey copper ore.',
    zn_pct=5.0, basis='value-text', years='1918', price_year=1918,
    commodities='Zinc, lead, silver')
row('Drilling Development Co. deposit (best area, 1918)',
    'Salmon City (Lemhi)', 'Lemhi', ['drilling development'],
    'Salmon, Lemhi Co.', 'ismir1918', 74, 79,
    'The best area of this immense', 'gold and\nsilver', usd_per_ton=3.0,
    basis='value-text', years='1918', price_year=1918,
    commodities='Gold, silver, zinc, lead')
row('Copper Basin mine (1918 leaser shipments)', 'Copper Basin (Custer)',
    'Custer', ['copper basin'], 'Mackay, Custer Co.', 'ismir1918', 75, 80,
    'leasers worker on the properties', 'ten per cent copper values.',
    cu_pct=10.0, basis='ore shipped', years='1918', price_year=1918,
    tonnage='several carloads crude', commodities='Copper')
row('Richmond mine (1918 shipments)',
    'East Coeur d\'Alene, St. Joe (Shoshone)', 'Shoshone', ['richmond'],
    'Mullan, Shoshone Co.', 'ismir1918', 76, 81,
    'The Richmond :Mine in the East', 'seven per\ncent copper.', cu_pct=7.0,
    basis='ore shipped', years='1918', price_year=1918,
    tonnage='nearly 3,000 tons crude', commodities='Copper')
row('Torney mine (Perrault Creek banded vein)', 'Salmon City (Lemhi)',
    'Lemhi', ['torney'], 'Salmon, Lemhi Co.', 'ismir1918', 77, 82,
    'another tunnel on this rnine', 'cent copper ore.', cu_pct=10.0,
    basis='assay-text', years='1918', price_year=1918, commodities='Copper')
row('Elmore Copper, Falun and Opportunity groups (Volcano district)',
    'Volcano (Elmore)', 'Elmore', ['elmore copper', 'falun', 'opportunity'],
    'Camas Prairie, Elmore Co.', 'ismir1918', 78, 83,
    'Eaeh of these properties carry', 'coppel\' valnes.', cu_pct=1.0,
    basis='assay-text', years='1918', price_year=1918,
    commodities='Copper, silver')
row('Hennessy antimony group (Yellow Pine district)', 'Yellow Pine (Valley)',
    'Valley', ['hennessy'], 'Stibnite, Valley Co.', 'ismir1918', 97, 102,
    'another Hennessy group of claims', 'twenty-five per cent antimony.',
    sb_pct=5.0, basis='assay-text', years='1918', price_year=1918,
    commodities='Antimony')
row('Fern Quicksilver mine (1918 furnace run)', 'Yellow Pine (Valley)',
    'Valley', ['fern'], 'Yellow Pine, Valley Co.', 'ismir1918', 96, 101,
    'made a I\'nn on the best ore', 'two pel\' cent quicksilver.',
    hg_flasks=22.5, basis='production', years='1918', price_year=1918,
    tonnage='summer furnace run, feed a little over 2% quicksilver',
    commodities='Mercury')
row('Sherman mine (No. 4 Union tunnel shoot)', 'Burke, Coeur d\'Alene (Shoshone)',
    'Shoshone', ['sherman'], 'Burke, Shoshone Co.', 'ismir1918', 55, 60,
    'On Canyon Creek, near Burke', 'below the apex of the vein.',
    pb_pct=20.0, basis='assay', years='1918', price_year=1918,
    commodities='Lead')
row('Big Creek district gold deposits', 'Big Creek-Edwardsburg (Valley)',
    'Valley', ['big creek'], 'Edwardsburg, Valley Co.', 'ismir1918', 83, 88,
    'The Big Creek district has a string', 'greatly increased gold supply',
    usd_per_ton=2.0, basis='value-text', years='1918', price_year=1918,
    commodities='Gold')
row('Merger Mines Company copper deposit (Hoodoo district)', 'Hoodoo (Latah)',
    'Latah', ['merger', 'mizpah'], 'Harvard, Latah Co.', 'ismir1918', 105, 110,
    'This same distl\'ict carries on the property', 'excellent concentrating ore', cu_pct=24.0,
    basis='ore shipped', years='1918', price_year=1918,
    tonnage='several cars crude', commodities='Copper')
row('Abundance mine (footwall pyrite test)',
    'Iron Mountain-Mineral (Washington)', 'Washington', ['abundance'],
    'Mineral, Washington Co.', 'ismir1918', 108, 113,
    'A fifteen foot cross section test', 'trace of gold.', cu_pct=1.5,
    ag_opt=1.5, basis='assay', years='1918', price_year=1918,
    commodities='Copper, silver, iron')
row('Barton mine (surface cut assay)', 'Iron Mountain-Mineral (Washington)',
    'Washington', ['barton'], 'Mineral, Washington Co.', 'ismir1918', 108, 113,
    'Another outcrop on this property', 'tw"elve feet wide.', cu_pct=6.0,
    ag_opt=6.0, basis='assay', years='1918', price_year=1918,
    commodities='Copper, silver, iron')

out = '/home/claude/nw/grades-research/agent_out/rows_ismir_r2.json'
json.dump(R, open(out, 'w'), indent=1)
print(f'{len(R)} rows written to {out}')
for r in R:
    print(f"  {r['src_key']} p.{r['page']:>3} {r['name'][:58]:<58} :: {r['quote'][:60]}")
