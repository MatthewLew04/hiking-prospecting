#!/usr/bin/env python3
"""CA cited gold grades -> spliced into site/data/grades/grades.json.

Curated the same way the NV/ID/MT/WA/OR/UT/WY set was built: verbatim
grade statements hand-extracted from the primary literature, one row per
mine (best cited grade), page-cited, with the quote carried in full.

Sources (all fetched + read for this dataset, 2026-08-08):
  PP 157  Knopf, The Mother Lode System of California (1929)
  PP 172  Ferguson & Gannett, Gold Quartz Veins of the Alleghany District (1932)
  PP 194  Johnston, The Gold Quartz Veins of Grass Valley (1940)
  B 430   Hess, Gold Mining in the Randsburg Quadrangle (1910)
  B 540   Ferguson, Gold Lodes of the Weaverville Quadrangle (1914)

$-per-ton -> oz/t conversion at $20.67/oz (all figures here predate 1934);
PP 194 states most grades directly in ounces. Bonanza/specimen-lot values
(basis 'assay-text'/'ore shipped') are the lot arithmetic from the quote,
same convention as the Nevada bonanza rows.

Coordinates: matched to MRDS CA sites by name within a district anchor
radius — never invented. No confident match => x=None (row documents the
mine but cannot score). 'open' = metres to nearest ACTIVE CA claim
centroid (5000 = none within ~5 km; -1 = unlocated), computed against
site/data/claims/ca_active.json.
"""
import json, math, os, re, sys

HERE = os.path.dirname(os.path.abspath(__file__))
SITE = os.path.join(HERE, '..', 'site')
OZ = 20.67          # $ per oz Au for every pre-1934 figure quoted here

PP157 = ('USGS Professional Paper 157, The Mother Lode System of California '
         '(Adolph Knopf, 1929)', 'https://pubs.usgs.gov/pp/0157/report.pdf')
PP172 = ('USGS Professional Paper 172, Gold Quartz Veins of the Alleghany '
         'District, California (H.G. Ferguson & R.W. Gannett, 1932)',
         'https://pubs.usgs.gov/pp/0172/report.pdf')
PP194 = ('USGS Professional Paper 194, The Gold Quartz Veins of Grass Valley, '
         'California (W.D. Johnston, Jr., 1940)',
         'https://pubs.usgs.gov/pp/0194/report.pdf')
B430 = ('USGS Bulletin 430, Gold Mining in the Randsburg Quadrangle, '
        'California (F.L. Hess, 1910)', 'https://pubs.usgs.gov/bul/0430/report.pdf')
B540 = ('USGS Bulletin 540, Gold Lodes of the Weaverville Quadrangle, '
        'California (H.G. Ferguson, 1914)', 'https://pubs.usgs.gov/bul/0540/report.pdf')

# district anchors (lon, lat) — for MRDS name-match search only
A = {'plymouth': (-120.845, 38.482), 'amador_city': (-120.824, 38.419),
     'sutter_creek': (-120.803, 38.393), 'jackson': (-120.774, 38.349),
     'san_andreas': (-120.680, 38.196), 'angels': (-120.539, 38.068),
     'carson_hill': (-120.535, 38.028), 'jamestown': (-120.423, 37.953),
     'tuttletown': (-120.464, 37.992), 'shawmut': (-120.400, 37.867),
     'placerville': (-120.800, 38.730), 'alleghany': (-120.843, 39.470),
     'forest': (-120.855, 39.478), 'grass_valley': (-121.061, 39.219),
     'randsburg': (-117.657, 35.370), 'whiskeytown': (-122.545, 40.635),
     'french_gulch': (-122.640, 40.700), 'deadwood': (-122.760, 40.730),
     'dog_creek': (-122.421, 40.939), 'dedrick': (-122.976, 40.867)}

# name, district, anchor, match-keys, exclude-substrings, au, usd, basis,
# yrs, page, source, quote, ton-note
R = []
def row(name, dist, anchor, keys, au, usd, basis, yrs, page, src,
        quote, ton=None, excl=()):
    R.append(dict(name=name, dist=dist, anchor=anchor, keys=keys, excl=excl,
                  au=au, usd=usd, basis=basis, yrs=yrs, page=page, src=src,
                  quote=quote, ton=ton))

# ---- PP 157 — Mother Lode (Knopf, 1929) --------------------------------
row('Eureka mine (Sutter Creek)', 'Mother Lode (Amador)', 'sutter_creek',
    ['old eureka', 'eureka'], round(20/OZ, 2), 20.0, 'production average',
    'ca. 1855-1860s', 'p. 12', PP157,
    'The average recovered value of the Eureka ore was $20 a ton.',
    excl=('central', 'south'))
row('Original Amador mine', 'Mother Lode (Amador)', 'amador_city',
    ['original amador'], round(28/OZ, 2), 28.0, 'production average', '1875',
    'p. 13', PP157,
    "Raymond's report for 1875: the lowest average yield, $6 a ton, was at "
    'the Phoenix mine at Plymouth; the highest average yield, $28 a ton, '
    'was at the Original Amador.')
row('Central Eureka mine (bonanza shoot, 1,100-1,900-foot levels)',
    'Mother Lode (Amador)', 'sutter_creek', ['central eureka'],
    round(70/OZ, 2), 70.0, 'value-text', 'pre-1900', 'p. 61', PP157,
    'At 1,100 feet the bonanza shoot came in, which extended down to the '
    '1,900-foot level with northerly pitch. The maximum stope length of '
    'this shoot was 700 feet. According to Storms a considerable quantity '
    'of the ore averaged $70 a ton.')
row('Keystone mine', 'Mother Lode (Amador)', 'amador_city', ['keystone'],
    round(18/OZ, 2), 18.0, 'production average', '1870s', 'p. 58', PP157,
    'Since 1870 its output had averaged $1,009 a day. The ore mined in '
    'those prosperous days yielded over yearly periods as much as $18 a '
    'ton.', ton='authenticated output $17,000,000 (R.C. Downs)')
row('Plymouth mine', 'Mother Lode (Amador)', 'plymouth',
    ['plymouth'], round(7.59/OZ, 2), 7.59, 'production average', '1887',
    'p. 49', PP157,
    'The average yield per ton was $6.18 in 1886 and $7.59 in 1887, when '
    '97,000 tons was crushed.',
    ton='1,020,845 tons milled to 1924 at $5.56/ton (p. 51)')
row('Oneida mine', 'Mother Lode (Amador)', 'jackson', ['oneida'],
    round(17.5/OZ, 2), 17.5, 'production average', 'pre-1900', 'p. 27', PP157,
    'At the Oneida, for example, the quartz within 6 or 8 feet of the '
    'hanging wall yielded from $30 to $40 a ton, but the whole vein, which '
    'was 10 to 40 feet wide, averaged $17.50 a ton.')
row('Bunker Hill mine', 'Mother Lode (Amador)', 'amador_city',
    ['bunker hill'], round(7.71/OZ, 2), 7.71, 'production average', '1914',
    'p. 56', PP157,
    'The ore as mined in 1914 ranged from $3.76 to $7.71 a ton. The $7.71 '
    'average was that of a block of 1,270 tons taken out from the 1,400 '
    'east gray-ore stope.')
row('South Eureka mine', 'Mother Lode (Amador)', 'sutter_creek',
    ['south eureka'], round(5.253/OZ, 2), 5.25, 'production average',
    'ca. 1915', 'p. 62', PP157,
    'The ore yielded $4.83 a ton, and the tailing loss was $0.423 a ton, '
    'so that the content of the ore was $5.253 a ton.')
row('Argonaut mine (south shoot footwall zone)', 'Mother Lode (Amador)',
    'jackson', ['argonaut'], round(8/OZ, 2), 8.0, 'assay', 'ca. 1924',
    'p. 67', PP157,
    'The footwall is indefinite, as the vein is underlain by a wide zone '
    'of stringers, which was said to assay $8 a ton over a width of 24 '
    'feet.', ton='output to 1924: $12,500,000 (p. 10)')
row('Morgan-Melones (Carson Hill Gold Mines)', 'Carson Hill (Calaveras)',
    'carson_hill', ['morgan', 'carson hill'], round(13.71/OZ, 2), 13.71,
    'production average', '1919', 'p. 73', PP157,
    'Output of Carson Gold Mines (Inc.): 1919 — 72,387 tons milled, yield '
    'per ton $13.71. From the high-grade hanging-wall ore body within the '
    'next few years was taken out more than $5,000,000; some ore assaying '
    'as much as $40 to $50 a ton was found in the bottom levels.',
    ton='Carson Hill total output reported $20,000,000')
row('Melones mine (4,125-foot level high-grade ore)',
    'Carson Hill (Calaveras)', 'carson_hill', ['melones'],
    round(50.20/OZ, 2), 50.20, 'assay', 'ca. 1924', 'p. 34', PP157,
    'Microscopic examination of high-grade ore, carrying $50.20 a ton, '
    'from the 4,125-foot level of the Melones mine showed it to contain '
    '90 to 95 per cent of ankerite.')
row('Utica mine', 'Angels Camp (Calaveras)', 'angels', ['utica'],
    round(2/OZ, 2), 2.0, 'assay-text', '1914', 'p. 37', PP157,
    'In 1914 I found gypsum in the ore on the 2,100-foot level of the '
    'Utica mine at Angels. The ore, which carries $2 in gold to the ton, '
    'consists chiefly of quartz, with minor dolomite, gypsum, and albite.',
    ton='Utica group total ~$7,000,000 by 1911 (p. 50)')
row('Dutch-Sweeney mine (footwall/App vein shoot)', 'Jamestown (Tuolumne)',
    'jamestown', ['dutch'], round(10/OZ, 2), 10.0, 'production average',
    '1893-1906', 'p. 78', PP157,
    'The early work was on a rich shoot in the footwall vein. This shoot '
    'was 490 feet long near the surface and ranged in width from 6 to 30 '
    'feet. It proved to be profitable down to the 400-foot level, '
    'averaging $10 a ton.')
row('Chilena mine (Jackass Hill)', 'Tuttletown (Tuolumne)', 'tuttletown',
    ['chilena'], round(9.2/OZ, 2), 9.20, 'value-text', '1924', 'p. 78',
    PP157,
    'An ore shoot 160 feet long has been developed in the zone. According '
    'to the superintendent, Mr. L. L. Coffer, the shoot averages $9.20 a '
    'ton. Telluride minerals are reported from some of the high-grade '
    'pockets.')
row('Eagle Shawmut mine', 'Mother Lode (Tuolumne)', 'shawmut',
    ['eagle shawmut', 'shawmut'], round(4/OZ, 2), 4.0,
    'production average', 'ca. 1916-1920', 'p. 80', PP157,
    'The average gold content of the ore, which was under $4 [a ton], '
    'with rising costs brought the shutdown of 1920.')
row('Guildford mine (Poverty Point)', 'Placerville (El Dorado)',
    'placerville', ['guildford', 'poverty point'], round(10/OZ, 2), 10.0,
    'value-text', '1915', 'p. 49', PP157,
    'The ore was reported to carry $10 a ton. The sulphide concentrate '
    'was said to carry from $35 to $180, depending on whether it was '
    'obtained from the ore shoot or not.')

# ---- PP 172 — Alleghany district (Ferguson & Gannett, 1932) ------------
row('Sixteen to One mine', 'Alleghany (Sierra)', 'alleghany',
    ['sixteen to one'], round(5000/(80/2000.0)/OZ, 1), None, 'assay-text',
    '1924-1928', 'p. 109', PP172,
    'One small shoot yielded nearly $1,000,000, and several others have '
    'yielded over $200,000 each. A tenor of $50 and over a pound is not '
    'unknown in the richest ore. A lot of 80 pounds from one of the rich '
    'shoots mined in 1924 yielded $5,000, and in 1928 a chunk of ore '
    'weighing 160 pounds netted $28,000.')
row('Rainbow mine', 'Alleghany (Sierra)', 'alleghany', ['rainbow'],
    round(116337/(1953/2000.0)/OZ, 1), None, 'ore shipped', '1881-1884',
    'p. 102', PP172,
    'As much as $60,000 was taken out in a single day and over $100,000 '
    'in a month. It is said that a shipment of 1,953 pounds yielded '
    '$116,337.', excl=('extension',))
row('Oriental mine', 'Alleghany (Sierra)', 'alleghany', ['oriental'],
    round(2000/OZ, 1), None, 'value-text', 'pre-1932', 'p. 100', PP172,
    'For much of the ore the yield was from about $1,000 to $2,000 a ton, '
    'much less than the usual return from the high-grade ore of the '
    'district. A shoot that yielded $736,000 from an area of 14 by 22 '
    'feet on the vein was mined in the Oriental mine (p. 52).',
    ton='workings yielded about $1,260,000 from about 5,000 feet of '
        'development')
row('Irelan mine', 'Alleghany (Sierra)', 'alleghany', ['irelan'],
    round(300000/OZ, 0), None, 'assay-text', 'pre-1932', 'p. 128', PP172,
    'The richest shoot, from which picked specimens are said to have '
    'assayed as high as $300,000 a ton, was a narrow streak crossing the '
    'vein diagonally to the north from footwall to hanging wall.')
row('Eldorado mine (Alleghany)', 'Alleghany (Sierra)', 'alleghany',
    ['eldorado', 'el dorado'], round(16.9/OZ, 2), 16.90,
    'production average', 'pre-1932', 'p. 118', PP172,
    'The reported production is about $325,000, equivalent to about $70 '
    'per foot of development on the vein. Records of tonnage milled in '
    'recent years show a recovery of $16.90 to the ton.')
row('Yellow Jacket (Colorado) mine', 'Alleghany (Sierra)', 'alleghany',
    ['yellow jacket', 'colorado'], round(15/OZ, 2), 15.0, 'value-text',
    'pre-1932', 'p. 119', PP172,
    'The ore mined from this shoot has a general tenor of $7 to $15 a '
    'ton, with patches of higher grade. Sulphides are relatively abundant '
    'and carry sufficient gold ($40 to $240 to the ton, averaging about '
    '$100).')
row('Golden King mine', 'Kanaka Creek (Sierra)', 'alleghany',
    ['golden king'], round(25/OZ, 2), 25.0, 'production average',
    '1890-1895', 'p. 123', PP172,
    'According to MacBoyle this drift is 800 feet in length, and the ore '
    'stoped yielded $25 a ton. The raise to the No. 3 level opened up 9 '
    'feet of $20 ore; at 50 feet a 12-inch ledge of solid sulphurets '
    'assaying $175 a ton was encountered.')
row('Plumbago mine', 'Kanaka Creek (Sierra)', 'alleghany', ['plumbago'],
    round(8.4/OZ, 2), 8.40, 'production average', 'ca. 1910-1930',
    'p. 130', PP172,
    'For the last 20 years the ratio of production to tonnage mined has '
    'been about $8.40 a ton.')
row('Brush Creek mine', 'Oregon Creek (Sierra)', 'forest', ['brush creek'],
    round(8.5/OZ, 2), 8.50, 'production average', 'pre-1932', 'p. 89',
    PP172,
    'Besides the high-grade bunches, much of the quartz, particularly '
    'where mixed with the carbonate and mariposite that have replaced the '
    'slate, yielded $8.50 a ton in the mill.')
row('Eureka mine (Oregon Creek, Alleghany district)',
    'Oregon Creek (Sierra)', 'forest', ['eureka'], round(500/OZ, 1), None,
    'value-text', 'pre-1932', 'p. 92', PP172,
    'The quartz showed no visible gold but contained abundant small '
    'pyramidal crystals of arsenopyrite and was said to have yielded from '
    '$300 to $500 a ton.', excl=('central', 'south'))
row('Diadem mine', 'Forest (Sierra)', 'forest', ['diadem'],
    round(1000/OZ, 1), None, 'ore shipped', '1899', 'p. 95', PP172,
    'The mine is said to have produced about $20,000. In 1899, 5 tons of '
    'high-grade ore, which yielded $5,000, was mined.')
row('North Fork mine', 'Forest (Sierra)', 'forest', ['north fork'], None,
    None, 'value-text', '1875-1877', 'p. 95', PP172,
    'A very rich shoot 75 to 100 feet in drift length was discovered... '
    'on May 31, 1875, $5,000 was taken out in eight hours. It is said '
    'that the total yield was $100,000, of which $40,000 was extracted '
    'with a hand mortar.')
row('Kate Hardy mine', 'Oregon Creek (Sierra)', 'forest', ['kate hardy'],
    None, None, 'value-text', 'since 1860', 'p. 91', PP172,
    'The largest pocket, said to have yielded $40,000, was mined from the '
    'surface on the hanging-wall side of the vein, near the creek. Total '
    'production is said to have been about $300,000.')
row('Osceola mine', 'Alleghany (Sierra)', 'alleghany', ['osceola'], None,
    None, 'value-text', 'pre-1895', 'p. 114', PP172,
    'Local reports give information of a shoot in the middle tunnel that '
    'yielded $30,000 and one in the lower tunnel that yielded $60,000. '
    'This must have been prior to 1895.')

# ---- PP 194 — Grass Valley (Johnston, 1940; oz figures direct) ---------
row('Empire mine', 'Grass Valley (Nevada)', 'grass_valley', ['empire'],
    0.56, None, 'production average', '1891-1928', 'p. 89', PP194,
    'The average gold content of the ore for the period 1891-1928 was '
    '0.56 ounce to the ton. Between 1891 and 1928 the yearly average '
    'value of the ore ranged between $5.10 and $23.80 a ton, with an '
    'average for the period of $11.58.',
    ton='value of the ore mined exceeded $30,000,000',
    excl=('west', 'north'))
row('North Star mine', 'Grass Valley (Nevada)', 'grass_valley',
    ['north star'], round(11.035/OZ, 2), 11.035, 'production average',
    'to 1929', 'p. 72', PP194,
    'The average value of ore mined from all veins in the North Star mine '
    'up to 1929 was $11.035 a ton, and the average for the North Star '
    'vein was somewhat higher. In 1893 the yield was over $31 per ton; '
    'that ore shoot produced in round numbers 250,000 tons yielding '
    '$5,250,000.')
row('Idaho-Maryland mine (Eureka-Idaho shoot)', 'Grass Valley (Nevada)',
    'grass_valley', ['idaho maryland', 'idaho'], 1.0, None,
    'production average', 'ca. 1893-1901', 'p. 94', PP194,
    'The ore came from the Eureka-Idaho shoot, which averaged 2.5 feet in '
    'width and yielded 1 ounce to the ton in gold; $5,000,000 was paid in '
    'dividends. Under the Maryland Co. (1893-1901) the average value per '
    'ton was $20.23. The sulphides yielded between 5 and 20 ounces to '
    'the ton in gold.')
row('Massachusetts Hill mine (Rocky Bar)', 'Grass Valley (Nevada)',
    'grass_valley', ['massachusetts hill', 'rocky bar'],
    round(15.8/OZ, 2), 15.80, 'production average', '1894-1901', 'p. 63',
    PP194,
    'The Massachusetts Hill mine, worked through the Old Rocky Bar deep '
    'shaft, was acquired by the North Star in 1894 and produced '
    '$1,078,075 from 68,222 tons of ore, an average of $15.80 a ton, in '
    'the period from 1894 to 1901.',
    ton='about $3,000,000 said to have been produced 1850-1866')
row('New York Hill mine', 'Grass Valley (Nevada)', 'grass_valley',
    ['new york hill'], round(49/OZ, 2), 49.0, 'production average',
    '1866-1867', 'p. 74', PP194,
    'Between 1852 and 1865 it produced $500,000, and in 1866-67, 2,189 '
    'tons of ore yielded $106,430, an average of $49 a ton. In 1882 the '
    'ore averaged $25 a ton.')
row('Allison Ranch mine', 'Grass Valley (Nevada)', 'grass_valley',
    ['allison ranch'], 2.5, None, 'production average', '1854-1866',
    'p. 82', PP194,
    'The mine was one of the principal producers in the period 1854-66, '
    'when 46,000 tons of ore averaging 2.5 ounces to the ton was mined. '
    'A footwall split, known as the Cariboo vein, contains 4 to 10 inches '
    'of quartz yielding some rich ore carrying 10 to 15 ounces of gold to '
    'the ton.')
row('Hartery mine', 'Grass Valley (Nevada)', 'grass_valley', ['hartery'],
    1.5, None, 'value-text', 'to 1893', 'p. 81', PP194,
    'The average gold content of the ore is reported to have been 1.5 '
    'ounces to the ton, mainly in free gold, with scattered bunches of '
    'high-grade ore.', ton='total production about $350,000 (MacBoyle)')
row('Wisconsin (Menlo) mine', 'Grass Valley (Nevada)', 'grass_valley',
    ['wisconsin', 'menlo'], 1.5, None, 'value-text', 'pre-1906', 'p. 82',
    PP194,
    'The average gold content of the ore in the upper level was around '
    '1.5 ounces to the ton, and the average gold content of concentrates, '
    'which made up 4 percent of the ore, was about 4.5 ounces to the ton.')
row('Omaha Consolidated mine', 'Grass Valley (Nevada)', 'grass_valley',
    ['omaha'], round(25/OZ, 2), 25.0, 'value-text', 'pre-1906', 'p. 81',
    PP194,
    'The average value of the ore from the stopes on the upper levels was '
    '$20 to $30 a ton. From 1890 to 1899 the Omaha Consolidated produced '
    '54,966 tons of ore valued at $883,970, an average of $6.17 a ton.')
row('Golden Center mine', 'Grass Valley (Nevada)', 'grass_valley',
    ['golden center'], 2.0, None, 'assay', '1933-1934', 'p. 67', PP194,
    'On the sublevel the shoot was about 135 feet long and averaged 14 '
    'inches of quartz that contained a little over 2 ounces of gold to '
    'the ton. A mill test of 79 tons of quartz from a stope above the '
    '500 level showed an average gold content of about 0.75 ounce to the '
    'ton.')
row('Pennsylvania mine', 'Grass Valley (Nevada)', 'grass_valley',
    ['pennsylvania'], round(35/OZ, 2), 35.0, 'production average',
    'pre-1940', 'p. 86', PP194,
    'The ore averaged $20 to $50 a ton. Pyrite and arsenopyrite, making '
    'up about 2 percent of the vein material, according to Lindgren '
    'average 2.25 to 4.25 ounces of gold to the ton of concentrate.')
row('Kate Hayes vein', 'Grass Valley (Nevada)', 'grass_valley',
    ['kate hayes'], round(42.5/OZ, 2), 42.5, 'production average', '1860s',
    'p. 86', PP194,
    'The Kate Hayes vein was worked in the sixties and is said to have '
    'produced $125,000 from ore averaging $35 to $50 a ton.')
row('Brock vein', 'Grass Valley (Nevada)', 'grass_valley', ['brock'], 4.0,
    None, 'production average', 'early workings; to 1931', 'p. 64', PP194,
    'Of this total, $12,000 came from early workings above 80 feet in '
    'depth, when the milled ore is said to have averaged 2 to 4 ounces to '
    'the ton. Recent workings to the third level have produced about '
    '$170,000 from ore averaging one-half to three-fourths ounce to the '
    'ton.')

# ---- B 430 — Randsburg (Hess, 1910) ------------------------------------
row('Yellow Aster mine', 'Rand (Kern)', 'randsburg', ['yellow aster'],
    round(100/OZ, 2), 100.0, 'value-text', 'pre-1910', 'p. 38', B430,
    'Parts of the veins have been rich, yielding ore reported to be worth '
    'over $100 per ton; but part has been of low tenor — less than $2 per '
    'ton.', ton='about $6,000,000 of the district total of $9,000,000-'
                '$10,000,000')
row('Kenyon mine', 'Rand (Kern)', 'randsburg', ['kenyon'],
    round(100/OZ, 2), 100.0, 'production average', 'pre-1910', 'p. 40',
    B430,
    'One lens in the Kenyon was 10 feet thick and averaged $100 to the '
    'ton. It was 40 to 50 feet wide. A rich stringer of ore found 300 '
    'feet southeast of the shaft carried $200 to $300 a ton.')
row('Baltic mine', 'Rand (Kern)', 'randsburg', ['baltic'],
    round(7/OZ, 2), 7.0, 'value-text', 'pre-1910', 'p. 41', B430,
    'In the Baltic ores said to carry about $7 a ton were taken out in '
    'one stope, which was 24 feet high and 60 to 70 feet broad.')
row('Buckboard mine', 'Rand (Kern)', 'randsburg', ['buckboard'],
    round(6/OZ, 2), 6.0, 'ore shipped', 'pre-1910', 'p. 41', B430,
    'A thousand tons of ore from the Buckboard was hauled to Johannesburg '
    'for milling tests and is reported to have run $6 a ton.')
row('Winnie claim', 'Stringer district, Rand (Kern)', 'randsburg',
    ['winnie'], round(140/OZ, 2), 140.0, 'assay', '1909', 'p. 44', B430,
    'At one place the quartz was 2 feet thick and gave $140 a ton on the '
    'plates. Where worked in November, 1909, the ore yielded about $50 '
    'per ton on the plates.')

# ---- B 540 — Weaverville quadrangle (Ferguson, 1914) -------------------
row('Mount Shasta mine', 'Whiskeytown (Shasta)', 'whiskeytown',
    ['mount shasta', 'mt shasta', 'mt. shasta'], round(42.69/OZ, 2),
    42.69, 'production average', '1897-1905', 'p. 47', B540,
    '88 tons of oxidized ore ran $48.44 to the ton. The Mount Shasta Gold '
    'Mines Corporation mined altogether from the first six levels a total '
    'of 4,072 tons, averaging $42.69 a ton, or $173,876.')
row('Mascot mine', 'Whiskeytown (Shasta)', 'whiskeytown', ['mascot'],
    round(11.85/OZ, 2), 11.85, 'assay', 'ca. 1912', 'p. 49', B540,
    'Two ore shoots have been prospected to some extent by raises. These '
    'are said to be each about 100 feet in length along the drift and to '
    'show a value of $11.85 a ton.')
row('Gambrinus mine', 'Whiskeytown (Shasta)', 'whiskeytown',
    ['gambrinus'], round(8/OZ, 2), 8.0, 'assay', 'ca. 1912', 'p. 50', B540,
    'Assays of this altered rock for the 27 feet between two of these '
    'veins is reported to have shown a tenor of $8 a ton, practically all '
    'of which was in the pyrite.')
row('Truscott mine', 'Whiskeytown (Shasta)', 'whiskeytown', ['truscott'],
    round(12.5/OZ, 2), 12.5, 'value-text', 'ca. 1912', 'p. 55', B540,
    'All the quartz is said to be workable and to carry from $10 to $15 a '
    'ton in free gold. The ore from one small vein, much mixed with '
    'altered porphyry and carrying much visible gold, is said to pan '
    'between $100 and $300 a ton.')
row('Gladstone mine', 'French Gulch (Shasta)', 'french_gulch',
    ['gladstone'], round(40/OZ, 2), 40.0, 'value-text', 'ca. 1912',
    'p. 60', B540,
    'The good ore of the ore shoots carries from $30 to $50 a ton, and '
    'some small stringers and patches may run up into the hundreds of '
    'dollars. This mine has been developed to a greater depth than any '
    'other in the district.')
row('Franklin mine', 'French Gulch (Shasta)', 'french_gulch', ['franklin'],
    round(45/OZ, 2), 45.0, 'value-text', 'ca. 1912', 'p. 63', B540,
    'The ore of the main vein, which runs at its best about $45 a ton, is '
    'in rather irregular pay shoots that pitch steeply to the south. The '
    'sulphides amount to about 0.75 per cent of the weight of the ore and '
    'carry $150 to the ton in gold.')
row('Washington mine', 'French Gulch (Shasta)', 'french_gulch',
    ['washington'], round(600/OZ, 1), 600.0, 'assay-text', 'early days',
    'p. 66', B540,
    'The oxidized ore that was first mined was exceedingly rich and ran '
    'as high as $600 a ton. The first mining work done consisted in '
    'sluicing the rich and decomposed material on the outcrop.')
row('Niagara mine', 'French Gulch (Shasta)', 'french_gulch', ['niagara'],
    round(423/OZ, 1), 423.0, 'ore shipped', 'ca. 1912', 'p. 67', B540,
    'Ore running as high as $150 a ton is found in small ore shoots 20 '
    'feet or less in length along the drift. The richest ore mined, '
    'according to Mr. Alexson, was one 3-ton lot that milled $423 a ton.')
row('Brunswick mine (French Gulch)', 'French Gulch (Shasta)',
    'french_gulch', ['brunswick'], round(10/OZ, 2), 10.0, 'value-text',
    'ca. 1912', 'p. 68', B540,
    'According to Mr. Lacey, the best ore so far found runs about $10 a '
    'ton.')
row('Brown Bear mine', 'Deadwood (Trinity)', 'deadwood', ['brown bear'],
    round(35/OZ, 2), 35.0, 'production average', '1909-1912', 'p. 71',
    B540,
    'The best ore of the present workings runs over $100 a ton and the '
    'general run of ore as stoped is between $20 and $50. The production '
    'of the district for 1909 was 2,415 tons valued at $31,094, and for '
    '1910, 2,723 tons valued at $49,158 — an average tenor of $17 to $18 '
    'a ton.')
row('Delta mine', 'Dog Creek (Shasta)', 'dog_creek', ['delta'],
    round(9/OZ, 2), 9.0, 'value-text', 'ca. 1912', 'p. 72', B540,
    'The workable ore is in irregular shoots and carries between $8 and '
    '$10 a ton in gold.')
row('Craig mine', 'Dedrick (Trinity)', 'dedrick', ['craig'],
    round(20/OZ, 2), 20.0, 'value-text', '1912', 'p. 78', B540,
    'It is said that the ore averages above $20 a ton but is extremely '
    'streaky and irregular. Visible gold is rarely seen. A 10-stamp mill '
    'and cyanide plant are being erected on the property.')


# ======================================================================
def canon(s):
    s = re.sub(r'[^a-z0-9 ]+', ' ', (s or '').lower().replace('-', ' '))
    s = re.sub(r'\b(the|mine|mines|mining|group|claim|claims|lode|quartz|'
               r'consolidated|con|no|inc|co|company)\b', ' ', s)
    return re.sub(r'\s+', ' ', s).strip()


def locate(rows):
    m = json.load(open(os.path.join(SITE, 'data/sites/mrds_ca.json')))
    pts = [(canon(m['nm'][i]), m['x'][i], m['y'][i]) for i in range(m['n'])
           if m['x'][i] is not None and m['nm'][i]]
    for r in rows:
        ax, ay = A[r['anchor']]
        coslat = math.cos(math.radians(ay))
        best, bs = None, 1e9
        for cn, x, y in pts:
            dk = math.hypot((x - ax) * 111.32 * coslat, (y - ay) * 111.32)
            if dk > 20:
                continue
            q = 0
            for k in r['keys']:
                ck = canon(k)
                if cn == ck:
                    q = 2; break
                if ck and ck in cn:
                    q = max(q, 1)
            if q == 0 or any(e in cn for e in r['excl']):
                continue
            score = dk - 25 * q
            if score < bs:
                bs, best = score, (x, y)
        r['x'], r['y'] = best if best else (None, None)
    hit = sum(1 for r in rows if r['x'] is not None)
    print(f'located {hit}/{len(rows)} via MRDS CA name match')
    for r in rows:
        if r['x'] is None:
            print(f"  UNLOCATED: {r['name']}")


def open_metres(rows):
    c = json.load(open(os.path.join(SITE, 'data/claims/ca_active.json')))
    assert c['layer'] == 'active' and c['state'] == 'CA'
    grid = {}
    for x, y in zip(c['x'], c['y']):
        if x is None:
            continue
        grid.setdefault((int(x / 0.02), int(y / 0.02)), []).append((x, y))
    print(f"open-distance vs {c['n']:,} active CA claims")
    for r in rows:
        if r['x'] is None:
            r['open'] = -1
            continue
        x, y = r['x'], r['y']
        coslat = math.cos(math.radians(y))
        gx, gy = int(x / 0.02), int(y / 0.02)
        best = 1e12
        for dx in (-3, -2, -1, 0, 1, 2, 3):
            for dy in (-3, -2, -1, 0, 1, 2, 3):
                for cx, cy in grid.get((gx + dx, gy + dy), ()):
                    d = math.hypot((cx - x) * 111320 * coslat,
                                   (cy - y) * 111320)
                    if d < best:
                        best = d
        r['open'] = 5000 if best > 5000 else int(round(best))


def splice(rows):
    p = os.path.join(SITE, 'data/grades/grades.json')
    g = json.load(open(p))
    # idempotent: drop any prior CA rows
    keep = [i for i in range(g['n']) if g['st'][i] != 'CA']
    cols = [k for k, v in g.items() if isinstance(v, list) and k != 'name'
            or k == 'name']
    cols = [k for k in g if isinstance(g[k], list)]
    for k in cols:
        g[k] = [g[k][i] for i in keep]
    dropped = g['n'] - len(keep)
    for r in rows:
        g['name'].append(r['name']); g['st'].append('CA')
        g['dist'].append(r['dist'])
        g['x'].append(round(r['x'], 5) if r['x'] is not None else None)
        g['y'].append(round(r['y'], 5) if r['y'] is not None else None)
        g['au'].append(r['au']); g['ag'].append(None)
        g['usd'].append(r['usd']); g['basis'].append(r['basis'])
        g['yrs'].append(r['yrs']); g['open'].append(r['open'])
        g['nrec'].append(1); g['dep'].append(None)
        g['quote'].append(r['quote'])
        g['src'].append(f"{r['src'][0]}, {r['page']}")
        g['url'].append(r['src'][1])
        g['ton'].append(r['ton']); g['ds'].append(None); g['wt'].append(None)
        g['pz'].append(None); g['com'].append(None); g['cnty'].append(None)
    g['n'] = len(g['name'])
    g['generated'] = '2026-08-08'
    if 'CA rows' not in g['note']:
        g['note'] += (' CA rows added 2026-08-08 from PP 157/172/194 + '
                      'Bulls 430/540 ($20.67/oz conversions, all figures '
                      'pre-1934); CA open distances vs ca_active.json.')
    json.dump(g, open(p, 'w'), separators=(',', ':'))
    from collections import Counter
    ca = [i for i in range(g['n']) if g['st'][i] == 'CA']
    rich = [i for i in ca if (g['au'][i] or 0) >= 0.3]
    ro = [i for i in rich if (g['open'][i] or 0) >= 400]
    print(f"grades.json: {g['n']} rows ({dropped} old CA dropped, "
          f"{len(ca)} CA added); CA rich {len(rich)}, rich+open {len(ro)}")


if __name__ == '__main__':
    locate(R)
    open_metres(R)
    splice(R)
