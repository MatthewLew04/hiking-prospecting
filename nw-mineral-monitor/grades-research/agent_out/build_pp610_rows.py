#!/usr/bin/env python3
"""Build WS9 round-2 district roll-up rows from PP 610 (CA + ID chapters).
Quotes are extracted verbatim from the cached page text via start/end anchors
so they validate exactly. printed page = pdf - 6 (verified); pdf_page set
explicitly on every row because printed_to_pdf mis-signs this book's offset."""
import json, os, re, sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                '..', '..', 'pipelines'))
import gradeslib as G

pt = G.load_pagetext('pp610')
PAGES = pt['pages']


def _flex(s):
    """Anchor -> regex: any whitespace run in the anchor matches \\s+."""
    parts = [re.escape(w) for w in s.split()]
    return r'\s+'.join(parts)


def span(pdf_page, start, end):
    """Exact text span from pdf page (1-based), whitespace-collapsed."""
    txt = PAGES[pdf_page - 1]
    m = re.search(_flex(start), txt)
    assert m, f'START not found on pdf {pdf_page}: {start[:60]!r}'
    m2 = re.search(_flex(end), txt[m.start():])
    assert m2, f'END not found on pdf {pdf_page}: {end[:60]!r}'
    q = txt[m.start():m.start() + m2.end()]
    q = re.sub(r'-\s*\n\s*', '', q)      # join printed hyphenation
    q = ' '.join(q.split())
    return q


def row(name, district, county, state, keys, anchor, quote_pdf, start, end,
        tonnage, commodities='Gold', years='through 1959', excl=None,
        au_opt=None, usd_per_ton=None, usd_per_yd3=None, plc=None,
        price_year=None, ag_opt=None, metal='Au'):
    pdf = quote_pdf
    return {
        'name': name,
        'district': district,
        'county': county,
        'state': state,
        'keys': keys,
        'excl': excl or [],
        'lat': None, 'lon': None,
        'anchor_hint': anchor,
        'metal': metal,
        'au_opt': au_opt, 'ag_opt': ag_opt, 'au_gpt': None,
        'usd_per_ton': usd_per_ton,
        'pb_pct': None, 'zn_pct': None, 'cu_pct': None, 'sb_pct': None,
        'wo3_units': None, 'hg_flasks': None,
        'usd_per_yd3': usd_per_yd3, 'plc': plc,
        'basis': 'district production',
        'years': years,
        'price_year': price_year,
        'tonnage': tonnage,
        'commodities': commodities,
        'src_key': 'pp610',
        'page': pdf - 6,
        'pdf_page': pdf,
        'quote': span(pdf, start, end),
    }


rows = []
R = rows.append

# ============================ CALIFORNIA ============================

R(row('Mother Lode district (Amador County)', 'Mother Lode (Amador)',
      'Amador', 'CA',
      ['mother lode'], 'Jackson-Sutter Creek, Amador Co.', 64,
      'Other important Mother Lode mines in Amador County',
      'through 1959 was about\n7,675,000 ounces.',
      '≈7,675,000 oz Au through 1959 (most productive Mother Lode county)',
      years='1850s-1959'))

R(row('Cosumnes River placers', 'Cosumnes River placers (Amador)', 'Amador',
      'CA', ['cosumnes'], 'Plymouth, Amador Co.', 64,
      'The U.S. Bureau of Mines (1933-66) reported',
      'roughly equivalent to 10,900 ounces of gold.',
      '2,125,000 yd3 dredged ≈ 10,900 oz Au (recent years pre-1954)',
      years='1932-54', usd_per_yd3=0.18, plc=1, price_year=1950))

R(row('Oroville district', 'Oroville (Butte)', 'Butte', 'CA',
      ['oroville'], 'Oroville, Butte Co.', 65,
      'The Quaternary flood-plain gravels of the Feather',
      'the largest producer of\nButte County.',
      '1,964,130 oz Au 1903-1959 (Feather River dredge field)',
      years='1903-59'))

R(row('Mother Lode, East Belt, and West Belt districts (Calaveras County)',
      'Mother Lode-East Belt-West Belt (Calaveras)', 'Calaveras', 'CA',
      ['mother lode', 'carson hill', 'angels'],
      'Angels Camp-Melones, Calaveras Co.', 66,
      'After 1950, lode mining in Calaveras County',
      'through\n1959 was 2,045,700 ounces.',
      '2,045,700 oz Au lode 1880-1959; early placers est. $50M more',
      years='1880-1959'))

R(row('Campo Seco district', 'Campo Seco (Calaveras)', 'Calaveras', 'CA',
      ['campo seco'], 'Campo Seco, Calaveras Co.', 66,
      'During that time an estimated 800,000 tons',
      'district was about 60,000 ounces.',
      '≈800,000 tons Pern mine copper ore 1899-1919; district total ≈60,000 oz Au',
      commodities='Gold, Copper', years='1899-1919', au_opt=0.03))

R(row('Mother Lode, East Belt, and West Belt districts (El Dorado County)',
      'Mother Lode-East Belt-West Belt (El Dorado)', 'El Dorado', 'CA',
      ['mother lode', 'placerville'], 'Placerville, El Dorado Co.', 67,
      'From 1903 through 1958 the lode mines of the district',
      'possibly be 1 million ounces\nor more.',
      '≈500,000 oz Au lode 1903-58; total possibly 1,000,000+ oz',
      years='1903-58'))

R(row('Cargo Muchacho district', 'Cargo Muchacho (Imperial)', 'Imperial',
      'CA', ['cargo muchacho', 'tumco'], 'Ogilby, Imperial Co.', 68,
      'Total production of the district to 1938 is conservatively',
      'but some was\nfrom dry placers.',
      '≈$4M (≈193,500 oz Au) to 1938 + 31,200 oz 1938-59',
      years='1879-1959'))

R(row('Rand district', 'Rand (Kern)', 'Kern', 'CA',
      ['rand', 'randsburg'], 'Randsburg, Kern Co.', 71,
      'This is the most important district in Kern County,',
      'silver has been a byproduct.',
      'district Au production through 1959: 836,300 oz (see quote 2 in tonnage note)',
      commodities='Gold, Silver', years='1893-1959'))
# amend Rand: single contiguous span carrying both district figures
rows[-1]['tonnage'] = ('836,300 oz Au through 1959, all but ≈1,700 oz '
                       'from lodes; ≈$9-10M ore mined before 1910')
rows[-1]['quote'] = span(71, 'Of the estimated $9 to',
                         'was\nfrom lode mines.')

R(row('Hornitos district', 'Hornitos (Mariposa)', 'Mariposa', 'CA',
      ['hornitos'], 'Hornitos, Mariposa Co.', 74,
      'The lode mines\nare all west of the Mother Lode,',
      'a reasonable estimate.',
      'minimum ≈500,000 oz Au (West Belt lodes + creek placers)',
      years='1850s-1959'))

R(row('Mother Lode and East Belt districts',
      'Mother Lode-East Belt (Mariposa)', 'Mariposa', 'CA',
      ['mother lode', 'coulterville'], 'Coulterville-Mariposa, Mariposa Co.',
      75,
      'Total gold production of the Mother Lode\nand East Belt',
      'approximately 1,009,000 ounces.',
      '≈1,009,000 oz Au through 1959',
      years='1849-1959'))

R(row('Bodie district', 'Bodie (Mono)', 'Mono', 'CA',
      ['bodie'], 'Bodie, Mono Co.', 76,
      'The Bodie district is in northeast Mono County,',
      'came from the Standard mine.',
      '1,456,300 oz Au total, 1860-1955',
      years='1860-1955'))

R(row('Grass Valley-Nevada City district',
      'Grass Valley-Nevada City (Nevada)', 'Nevada', 'CA',
      ['grass valley', 'nevada city'], 'Grass Valley, Nevada Co.', 77,
      "Of the estimated $113 million worth of production",
      '2,200,000 ounces of placer gold.',
      '≈10,408,000 oz lode + 2,200,000 oz placer Au through 1959',
      years='1849-1959'))

R(row('La Porte district', 'La Porte (Plumas)', 'Plumas', 'CA',
      ['la porte', 'laporte'], 'La Porte, Plumas Co.', 80,
      'The La Porte district, in T. 21 N., R. 9 E.,',
      'was about\n2,910,000 ounces.',
      '≈2,910,000 oz Au 1855-1959 (hydraulic/drift, Tertiary Yuba channel)',
      years='1855-1959'))

R(row('Folsom district', 'Folsom (Sacramento)', 'Sacramento', 'CA',
      ['folsom'], 'Folsom, Sacramento Co.', 81,
      'Before 1930 some drift mines were operating,',
      'was\nat least 3 million ounces.',
      '≥3,000,000 oz Au through 1959 (American River dredge field)',
      years='1899-1959'))

R(row('Julian district', 'Julian (San Diego)', 'San Diego', 'CA',
      ['julian', 'banner'], 'Julian, San Diego Co.', 83,
      'The total gold production of San Diego County',
      'scattered\nthroughout the county.',
      'county total ≈219,800 oz Au through 1959, mostly Julian district',
      years='1870-1959'))

R(row('Deadwood-French Gulch district',
      'Deadwood-French Gulch (Shasta)', 'Shasta', 'CA',
      ['french gulch', 'deadwood'], 'French Gulch, Shasta Co.', 84,
      'Ferguson (1914, p. 55) reported a production',
      'mostly from lode mines.',
      '≈128,900 oz Au through 1959 ($1,607,764 through 1911)',
      years='1852-1959'))

R(row('Harrison Gulch district', 'Harrison Gulch (Shasta)', 'Shasta', 'CA',
      ['harrison gulch', 'midas'], 'Knob, Shasta Co.', 84,
      'The total production for the district was about $4 million',
      'placer production is recorded in this\ndistrict.',
      '≈$4M total (Midas mine bulk of output, 1894-1920)',
      years='1894-1920'))

R(row('Alleghany and Downieville districts',
      'Alleghany-Downieville (Sierra)', 'Sierra', 'CA',
      ['alleghany', 'downieville'], 'Alleghany, Sierra Co.', 85,
      'Total minimum production through 1959, including',
      'was\nabout 2,173,000 ounces.',
      '≈2,173,000 oz Au total (lode 1,590,990 oz through 1959)',
      years='1852-1959'))

R(row('Sierra Buttes district', 'Sierra Buttes (Sierra)', 'Sierra', 'CA',
      ['sierra buttes', 'sierra city'], 'Sierra City, Sierra Co.', 86,
      'The most important mine was the Sierra\nButtes,',
      'from the Sierra\nButtes mine.',
      '≈825,000 oz Au total, nearly all Sierra Buttes mine',
      years='1850s-1959'))

R(row('Salmon River district', 'Salmon River (Siskiyou)', 'Siskiyou', 'CA',
      ['salmon river', 'sawyers bar'], 'Sawyers Bar, Siskiyou Co.', 87,
      'The Quaternary placers between Sawyers Bar',
      'most productive in\nSiskiyou County.',
      '≈$25M placer Au (most productive Siskiyou district)',
      years='1850s-1959'))

R(row('Klamath River district', 'Klamath River (Siskiyou)', 'Siskiyou', 'CA',
      ['klamath river', 'happy camp'], 'Happy Camp, Siskiyou Co.', 87,
      'From 1933 through 1959 the district produced 53,619',
      'earlier production.',
      '53,619 oz lode + 140,364 oz placer Au 1933-59 (earlier unrecorded)',
      years='1933-59'))

R(row('Trinity River basin district', 'Trinity River basin (Trinity)',
      'Trinity', 'CA', ['trinity river', 'weaverville', 'la grange'],
      'Weaverville, Trinity Co.', 88,
      'The La Grange mine near Weaverville',
      'was\nabout 1, 750,000 ounces.',
      '≈1,750,000 oz placer Au 1880-1959 (hydraulic + dragline dredges)',
      years='1880-1959'))

R(row('Carrville district', 'Carrville (Trinity)', 'Trinity', 'CA',
      ['carrville'], 'Trinity Center, Trinity Co.', 88,
      'Almost its entire production has come from lode',
      'reported in recent years.',
      '≈$1M lode Au through 1910',
      years='pre-1910'))

R(row('Columbia Basin-Jamestown-Sonora district',
      'Columbia Basin-Jamestown-Sonora (Tuolumne)', 'Tuolumne', 'CA',
      ['columbia', 'jamestown', 'sonora'], 'Columbia-Sonora, Tuolumne Co.',
      89,
      'Total gold production from this area was about $121',
      '(Julihn and Horton, 1940,\np. 69).',
      '≈$121M (≈5,874,000 oz) placer Au, chiefly 1853-70s',
      years='1853-1959'))

R(row('Mother Lode district (Tuolumne County)', 'Mother Lode (Tuolumne)',
      'Tuolumne', 'CA',
      ['mother lode', 'tuttletown'], 'Tuttletown-Jacksonville, Tuolumne Co.',
      90,
      'The placer production is probably from Tertiary',
      'about 1,550,000 ounces.',
      'minimum ≈1,550,000 oz Au total',
      years='1850-1959'))

R(row('East Belt district', 'East Belt (Tuolumne)', 'Tuolumne', 'CA',
      ['east belt', 'soulsby'], 'Soulsbyville, Tuolumne Co.', 89,
      'Nevertheless, the Soulsby mine has produced',
      'was about 965,000 ounces.',
      '≈965,000 oz Au through 1959 ($19,340,000 before 1899)',
      years='1850s-1959'))

R(row('Hammonton district', 'Hammonton (Yuba)', 'Yuba', 'CA',
      ['hammonton'], 'Hammonton, Yuba Co.', 90,
      'Beginning in 1903, large-scale dredging',
      'was about 4,387,100\nounces.',
      '≈4,387,100 oz Au 1903-59 (Yuba River dredge field, ≈$100M by 1949)',
      years='1903-59'))

# ============================== IDAHO ==============================

R(row('Black Hornet district', 'Black Hornet (Ada)', 'Ada', 'ID',
      ['black hornet'], 'Boise, Ada Co. (Boise Ridge)', 127,
      'Total recorded production from 1880 through 1959 was 21,431',
      'was 21,431 ounces.',
      '21,431 oz Au 1880-1959',
      years='1880-1959'))

R(row('Camas district', 'Camas (Blaine)', 'Blaine', 'ID',
      ['camas', 'mineral hill', 'hailey'], 'Hailey, Blaine Co.', 127,
      'Production records are not complete, especially',
      'came\nfrom the Camas district.',
      'more than half of Blaine Co. 175,770 oz Au 1874-1900; district total ≈102,000 oz',
      commodities='Gold, Silver, Lead, Zinc', years='1874-1959',
      excl=['camas county']))

R(row('Boise Basin district', 'Boise Basin (Boise)', 'Boise', 'ID',
      ['boise basin', 'idaho city', 'centerville'], 'Idaho City, Boise Co.',
      130,
      'Total gold production for the\ncounty from 1863 through 1959',
      'came from the\nBoise Basin.',
      'county 2,891,530 oz Au 1863-1959, ≈95% from Boise Basin '
      '(district total ≈2,300,000 oz, mostly placer)',
      years='1862-1959'))

R(row('Pioneerville district', 'Pioneerville (Boise)', 'Boise', 'ID',
      ['pioneerville', 'grimes pass'], 'Pioneerville, Boise Co.', 131,
      'The Golden Age mine produced ore worth $200,000',
      'through 1959 was about 25,000 ounces.',
      '≈25,000 oz Au 1895-1959',
      commodities='Gold, Silver, Lead', years='1895-1959'))

R(row('Quartzburg district', 'Quartzburg (Boise)', 'Boise', 'ID',
      ['quartzburg', 'gold hill', 'placerville'], 'Quartzburg, Boise Co.',
      131,
      'Ross (1941, p. 20) mentioned a total of $8 million',
      'idle from 1940\nthrough 1959.',
      '≈$8M (≈400,000 oz) Au total',
      years='1863-1940'))

R(row('Snake River placers (Cassia County)',
      'Snake River placers (Cassia)', 'Cassia', 'ID',
      ['snake river'], 'Snake River, Cassia Co.', 132,
      'At any rate, Staley (1946, p. 30) credited',
      'to Minidoka County.',
      '≈22,000 oz placer Au (Cassia County share)',
      years='pre-1942'))

R(row('Pierce district', 'Pierce (Clearwater)', 'Clearwater', 'ID',
      ['pierce', 'orofino'], 'Pierce, Clearwater Co.', 133,
      'The placers were worked on a moderate scale',
      'was about 385,000 ounces.',
      '≈385,000 oz Au through 1959 (first big Idaho placer camp, 1860)',
      years='1860-1959'))

R(row('Alder Creek district', 'Alder Creek (Custer)', 'Custer', 'ID',
      ['alder creek', 'mackay', 'empire'], 'Mackay, Custer Co.', 133,
      'From 1884 to 1913 the Empire produced',
      'through 1959 was\nabout 33,500 ounces.',
      '≈33,500 oz byproduct Au through 1959 (Empire copper mine)',
      commodities='Gold, Copper', years='1884-1959'))

R(row('Yankee Fork district', 'Yankee Fork (Custer)', 'Custer', 'ID',
      ['yankee fork', 'custer', 'bonanza'], 'Bonanza-Custer, Custer Co.', 134,
      'Anderson (1949, p. 14) credited the district',
      'through 1959 was about 266,600 ounces.',
      '$13M Au+Ag to 1948 ($12M pre-1910); ≈266,600 oz Au through 1959',
      commodities='Gold, Silver', years='1875-1959'))

R(row('Atlanta district', 'Atlanta (Elmore)', 'Elmore', 'ID',
      ['atlanta'], 'Atlanta, Elmore Co.', 135,
      'According to Ross (1941, p. 51), total metal production',
      'may have\nbeen 385,000 ounces.',
      '243,175 oz Au 1932-59; total possibly ≈385,000 oz',
      commodities='Gold, Silver', years='1864-1959'))

R(row('Rocky Bar district', 'Rocky Bar (Elmore)', 'Elmore', 'ID',
      ['rocky bar', 'elmore'], 'Rocky Bar, Elmore Co.', 136,
      'According to Ross (1941, p. 47), the quartz mines',
      'Pittsburg mines alone.',
      '≈$2M lode (Au+Ag) + ≈$2M placer Au to 1882',
      commodities='Gold, Silver', years='1863-1882'))

R(row('Elk City district', 'Elk City (Idaho)', 'Idaho', 'ID',
      ['elk city', 'buster'], 'Elk City, Idaho Co.', 138,
      'The early gold production of the district was estimated',
      'was about 550,000 to 800,000 ounces.',
      '≈550,000-800,000 oz Au total (placers from 1861 + lodes)',
      years='1861-1959'))

R(row('French Creek-Florence district', 'French Creek-Florence (Idaho)',
      'Idaho', 'ID', ['florence', 'french creek'],
      'Florence (Grangeville), Idaho Co.', 138,
      'The total output of the district, most of which',
      'from the early placers.',
      '$15-30M (≈1,000,000 oz Au), nearly all 1860s placers',
      years='1861-1959'))

R(row('Warren-Marshall district', 'Warren-Marshall (Idaho)', 'Idaho', 'ID',
      ['warren'], 'Warren, Idaho Co.', 139,
      'From 1936 through 1959 the district',
      'was about 906,500 ounces.',
      '≈906,500 oz Au total (≈$15M pre-1900, mostly placer)',
      years='1862-1959'))

R(row('Tenmile district', 'Tenmile (Idaho)', 'Idaho', 'ID',
      ['tenmile', 'newsome'], 'Newsome, Idaho Co.', 139,
      'Gold was discovered in 1861 in Newsome basin',
      'was about 147,000\nounces',
      '≈147,000 oz Au total (≈$2M Newsome Creek placers)',
      years='1861-1959'))

R(row('Mackinaw (Leesburg) district', 'Mackinaw-Leesburg (Lemhi)', 'Lemhi',
      'ID', ['leesburg', 'mackinaw', 'napias'], 'Leesburg, Lemhi Co.', 142,
      'Umpleby (1913b, p. 146) estimated the value of placer',
      'was about\n271,200 ounces.',
      '≈271,200 oz Au total (placers ≤$5M, lodes ≈$250,000)',
      years='1866-1959'))

R(row('Silver City district', 'Silver City (Owyhee)', 'Owyhee', 'ID',
      ['silver city', 'war eagle', 'florida mountain'],
      'Silver City, Owyhee Co.', 144,
      'Piper (in Piper and Laney, 1926, p. 58) estimated',
      "change Ross' estimate.",
      '>1,000,000 oz Au total (Owyhee County 1,103,545 oz 1863-1959)',
      commodities='Gold, Silver', years='1863-1959'))

R(row('Silver City district (second boom, De Lamar camp)',
      'Silver City (Owyhee)', 'Owyhee', 'ID',
      ['de lamar', 'delamar'], 'De Lamar (Wagontown), Owyhee Co.', 144,
      'In 1889, discoveries at the Black Jack mine',
      '(Piper and Laney, 1926, p. 55-56).',
      '$23M precious metals 1889-1914 (De Lamar-Florida Mountain boom)',
      commodities='Gold, Silver', years='1889-1914'))

R(row("Coeur d'Alene region", "Coeur d'Alene (Shoshone)",
      'Shoshone', 'ID', ['coeur', 'murray'],
      'Murray-Wallace, Shoshone Co.', 145,
      "Mines in the Coeur d'Alene region, including",
      '(Shenon, 1961, p. 1).',
      '≈439,000 oz Au through 1960 ($7,180,151 = 348,550 oz 1884-1931); '
      'gold leg = Murray placers + Prichard Fm veins',
      commodities='Gold, Silver, Lead, Zinc', years='1884-1960'))

R(row('Thunder Mountain district', 'Thunder Mountain (Valley)', 'Valley',
      'ID', ['thunder mountain', 'dewey', 'sunnyside'],
      'Roosevelt (Monumental Creek), Valley Co.', 146,
      'The total value of production of the district to about 1940',
      'probably about 17,500\nounces.',
      '≈$400,000 total to 1940; ≈17,500 oz Au through 1959',
      commodities='Gold, Silver', years='1902-1959'))

R(row('Yellow Pine district', 'Yellow Pine (Valley)', 'Valley', 'ID',
      ['yellow pine', 'stibnite', 'meadow creek'], 'Stibnite, Valley Co.',
      147,
      'The gold production of the Yellow Pine\nand Meadow Creek',
      'through 1959 was\n309,734 ounces.',
      '309,734 oz Au through 1959 (101,437 oz Yellow Pine + Meadow Creek '
      'mines through 1945)',
      commodities='Gold, Antimony, Tungsten, Mercury', years='1900-1959'))

# ---- verify every quote is exactly on its page ----
bad = 0
for r in rows:
    s = G.quote_on_page(r['quote'], PAGES[r['pdf_page'] - 1])
    if s < 1.0:
        bad += 1
        print('WEAK', round(s, 3), r['name'], r['pdf_page'])
print(f'{len(rows)} rows built, {bad} weak quotes')

out = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                   'rows_pp610.json')
json.dump(rows, open(out, 'w'), indent=1, ensure_ascii=False)
print('wrote', out)
