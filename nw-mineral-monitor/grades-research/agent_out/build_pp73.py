#!/usr/bin/env python3
# Build rows_pp73.json for WS9 round 2 — Lindgren PP 73 placer grades.
import json, sys, os
sys.path.insert(0, '/home/claude/nw/pipelines')
import gradeslib as G

R = []
def row(name, district, county, keys, anchor, pdf_page, page, quote,
        yd=None, ton=None, basis="value-text", years="pre-1911",
        price_year=1910, tonnage=None, excl=None):
    R.append({
        "name": name, "district": district, "county": county, "state": "CA",
        "keys": keys, "excl": excl or [], "lat": None, "lon": None,
        "anchor_hint": anchor, "metal": "Au",
        "au_opt": None, "ag_opt": None, "au_gpt": None,
        "usd_per_ton": ton, "pb_pct": None, "zn_pct": None, "cu_pct": None,
        "sb_pct": None, "wo3_units": None, "hg_flasks": None,
        "usd_per_yd3": yd, "plc": 1, "basis": basis, "years": years,
        "price_year": price_year, "tonnage": tonnage, "commodities": "Gold",
        "src_key": "pp73", "page": page, "pdf_page": pdf_page, "quote": quote})

# ---------------- Butte County ----------------
row("Morris Ravine drift mine (near Oroville)", "Oroville (Butte)", "Butte",
    ["morris ravine"], "Oroville, Butte Co.", 115, 89,
    "Coarse gold was found, some pieces having a value up to $133. About 12,000 cubic yards was mined, containing according to reports from $4 to $9 a yard.",
    yd=4.0, basis="production", years="pre-1897", price_year=1897,
    tonnage="about 12,000 cubic yards mined (drifting closed 1897)")

row("Cherokee hydraulic mine (lower part of white quartzose gravel)",
    "Cherokee (Butte)", "Butte", ["cherokee"], "Cherokee, Butte Co.", 111, 87,
    "White' sand and quartzose gravel, 50 feet thick, mostly very fine, some a little coarser, cobbles on bedrock of e. Lower part yields 25 cents to the cubic yard in fine gold.",
    yd=0.25, basis="value-text", years="pre-1911", price_year=1905,
    excl=["san juan", "nevada"])

row("Oroville dredging ground (Feather River)", "Oroville (Butte)", "Butte",
    ["oroville"], "Oroville, Butte Co.", 115, 89,
    "The gold is fine, and some of it may be called flour gold. The tenor ranges from 12 to 20 cents to the cubic yard.",
    yd=0.12, basis="value-text", years="circa 1901-1910", price_year=1905)

# ---------------- Plumas County ----------------
row("Pine Leaf channel (Pine Leaf and Kniewel mines)", "Meadow Valley (Plumas)",
    "Plumas", ["pine leaf", "kniewel"], "Spanish Ranch, Plumas Co.", 124, 98,
    "This channel is 200 feet wide and its gravels, which are only 4 feet thick, are stated to contain about $1.50 in gold to the cubic yard. They are covered by sandy pipe clay, above which lie masses of volcanic gravel. The channel has been worked at the Pine Leaf and Kniewel mines",
    yd=1.50, basis="value-text", years="pre-1911", price_year=1910)

row("La Porte diggings (bank of gravel cited by Pettee)", "La Porte (Plumas)",
    "Plumas", ["la porte", "laporte"], "La Porte, Plumas Co.", 129, 103,
    "The lower gravels in the principal channels are apt to be rich', ranging from $2 up to $20 or more to the cubic yard. Pettee cites a bank of gravel at La Porte, 250 by 100 feet and 30 feet high, that yielded at the rate of $20.87 to the cubic yard. Doubtless most of the gold was obtained near the bedrock.",
    yd=20.87, basis="production average", years="pre-1877", price_year=1875)

row("Niagara drift mine (Hepsidam, Gibsonville channel)",
    "Gibsonville-Hepsidam (Plumas)", "Plumas", ["niagara"],
    "Hepsidam, near Gibsonville", 134, 106,
    "At the Niagara mine, at Hepsidam, the channel was 800 feet wide between rims; of this a width of 500 or 600 feet was drifted. The gold on the bedrock was coarse, but the upper gravels in places contained pay. The tenor of much of the drifted ground was $3 a cubic yard.",
    yd=3.0, basis="production average", years="1875-1895", price_year=1885)

# ---------------- Sierra County ----------------
row("Poverty Hill drifting ground (La Porte channel)", "Poverty Hill-Scales (Sierra)",
    "Sierra", ["poverty hill"], "Poverty Hill, Sierra Co.", 126, 100,
    "In 1906 and 1907 the gravels of Poverty Hill were prospected with a view to drifting operations. The deep channel is about 150 feet wide. The gravel is only a few feet thick and along the rims thins out to less than 2 feet. It is covered by sand. The gold, which is moderately fine, is distributed through a thickness of 5 or 6 feet and the gravels are said to average $2 to the cubic yard.",
    yd=2.0, basis="value-text", years="1906-1907", price_year=1907)

row("Brandy City hydraulic mine", "Brandy City (Sierra)", "Sierra",
    ["brandy city"], "Brandy City, Sierra Co.", 127, 101,
    "Aside from the overlying lava ash, the whole deposit is pay gravel carrying near the surface about 10 cents in gold per cubic yard, and near the bedrock as high as $2.50 per cubic yard. In the spring of 1909 about 30,000 cubic yards of the upper part of the gravel was hydraulicked and 10 cents per cubic yard recovered. This gravel came from the east rim of the channel, 60 feet above bedrock. The general average of the gravel is estimated, from the old records, as 25 cents per cubic yard.",
    yd=0.25, basis="production average", years="pre-1891 (old records)", price_year=1890,
    tonnage="about 10,000,000 cubic yards yet to be mined")

row("Brandy City hydraulic mine (upper gravel, 1909 run)", "Brandy City (Sierra)",
    "Sierra", ["brandy city"], "Brandy City, Sierra Co.", 127, 101,
    "In the spring of 1909 about 30,000 cubic yards of the upper part of the gravel was hydraulicked and 10 cents per cubic yard recovered. This gravel came from the east rim of the channel, 60 feet above bedrock.",
    yd=0.10, basis="production", years="1909", price_year=1909,
    tonnage="30,000 cubic yards washed, spring 1909")

row("Bald Mountain drift mine (Forest)", "Alleghany-Forest (Sierra)", "Sierra",
    ["bald mountain"], "Forest (Forest City), Sierra Co.", 175, 142,
    "It was worked by the Bald Mounta in Co. from 1872 to 1879 or 1880 for a distanc e of about a mile, produci ng $150,00 0. The gravel was extract ed to a height of 3-! feet, includin g 1 foot of bedrock. The yield per cubic yard of unbrok en gravel was about $7.",
    yd=7.0, basis="production average", years="1872-1880", price_year=1876,
    tonnage="produced $150,000 from about a mile of channel", excl=["quartz"])

# ---------------- Nevada County ----------------
row("North Bloomfield hydraulic mine", "North Bloomfield (Nevada)", "Nevada",
    ["north bloomfield", "bloomfield"], "North Bloomfield, Nevada Co.", 171, 139,
    "The average yield per cubic yard is from 4 to 10 cents. Most of the value is contained in the deep gravels (130 feet), and in these the richest parts are the first few feet above the bedrock. Some portions of the clay and sand near the top are almost barren. Owing to the great width of the channel the gravel next to the bedrock is rarely rich enough for drifting. The yield of the mine from 1866 to 1900 was approximately $3,500,000.",
    yd=0.04, basis="production average", years="1866-1900", price_year=1885,
    tonnage="yield 1866-1900 approximately $3,500,000")

row("Derbec drift mine (North Bloomfield channel)", "North Bloomfield (Nevada)",
    "Nevada", ["derbec"], "North Bloomfield, Nevada Co.", 172, 140,
    "The Derbec channel, which has a steep grade, has been mined upstream from the shaft for a distance of 7,000 feet, following the curves; the width of pay gravel was from 150 to 600 feet and the height 8 to 16 feet from the-bedrock. The gravel is coarse, with many bowlders, some of which are of granite. The average value per ton is $2.47. The mine was in operation from 1877 to 1893, and the production in some years reached $200,000.",
    ton=2.47, basis="production average", years="1877-1893", price_year=1885,
    tonnage="production in some years reached $200,000")

row("Omega hydraulic diggings", "Washington-Omega (Nevada)", "Nevada",
    ["omega"], "Omega, Nevada Co.", 186, 147,
    "Extensive hydraulic operations have removed 12,000,000 cubic yards at Omega, the tailings being discharged in Scotchman Creek through a 3,000-foot bedrock tunnel. Apparently reliable calculations give 13! cents as the yield per cubic yard, the lowest gravel, of course, being niuch the richest part of the deposit.",
    yd=0.135, basis="production average", years="pre-1911", price_year=1890,
    tonnage="12,000,000 cubic yards removed")

row("Blue Tent hydraulic mine (lower gravel)", "Blue Tent (Nevada)", "Nevada",
    ["blue tent"], "Blue Tent, near Nevada City", 176, 143,
    "About 15,000,000 cubic yards has been removed and some 90,000,000 yards remain, much of which is barren clay and sand. The lower gravel averaged 15 cents or more to the cubic yard, but the sandy top gravel contained only 2t cents. It is stated that the hydraulic operations were not remunerative.",
    yd=0.15, basis="production average", years="pre-1911", price_year=1880,
    tonnage="about 15,000,000 cubic yards removed")

row("American Hill diggings (below North San Juan)", "North San Juan (Nevada)",
    "Nevada", ["american hill"], "North San Juan, Nevada Co.", 150, 122,
    "At American Hill, below North San Juan, the channel has been worked for 3,000 feet, the width from rim to rim being about 1,000 feet, the thickness averaging 150 feet. The gross product from 1860 to 1872 was, according to Whitney, $1,241,240. Pettee says that the gravel averaged 30 cents to the cubic yard.",
    yd=0.30, basis="production average", years="1860-1872", price_year=1866,
    tonnage="gross product 1860-1872 $1,241,240")

row("San Juan Hill diggings (lower end)", "North San Juan (Nevada)", "Nevada",
    ["san juan hill", "san juan"], "North San Juan, Nevada Co.", 150, 122,
    "The lower end of the. San Juan Hill yielded $157,000 in 1858, the contents averaging 35 cents to the cubic yard. This includes the bottom gravel, which is much richer than the top.",
    yd=0.35, basis="production average", years="1858", price_year=1858,
    tonnage="yielded $157,000 in 1858")

row("Cherokee diggings, San Juan Ridge (top gravels)", "North San Juan (Nevada)",
    "Nevada", ["cherokee"], "Cherokee (Patterson), near North San Juan", 150, 122,
    "The thick gravels from Cherokee to French Corral contain gold throughout; even the top gravels at Cherokee are profitable by the hydraulic method and yield 10 to 15 cents a cubic yard in fine gold.",
    yd=0.10, basis="value-text", years="pre-1877", price_year=1875,
    excl=["butte"])

# ---------------- Yuba County ----------------
row("Smartsville diggings", "Smartsville (Yuba)", "Yuba", ["smartsville"],
    "Smartsville, Yuba Co.", 150, 122,
    "The total yield of the Smartsville diggings up to 1877 is estimated by Pettee to have been $13,000,000; the average yield was probably 37 cents a cubic yard.",
    yd=0.37, basis="district production", years="to 1877", price_year=1875,
    tonnage="total yield to 1877 about $13,000,000")

# ---------------- Placer County ----------------
row("Polar Star hydraulic mine (Dutch Flat)", "Dutch Flat (Placer)", "Placer",
    ["polar star"], "Dutch Flat, Placer Co.", 177, 144,
    "Practically the whole extent of the channel has been drifted and the cemented. gravel worked in stamp mills. The yield is not known but probably exceeds $3,000,000. The Polar Star hydraulic gravel is said to average 11 cents to the cubic yard.",
    yd=0.11, basis="value-text", years="pre-1911", price_year=1900)

row("Gold Run hydraulic mines (Gold Run-Indiana Hill)", "Gold Run (Placer)",
    "Placer", ["gold run"], "Gold Run, Placer Co.", 184, 145,
    "An area of 555 acres has been washed off to an average depth of 75 feet. At Indiana Hill, where the bedrock elevation is 2,792 feet, the bottom gravel was drifted and crushed in mills. The yield per cubic yard of hydraulic gravel is said to be 11 cents.",
    yd=0.11, basis="value-text", years="circa 1865-1882", price_year=1875,
    tonnage="some 84,000,000 cubic yards washed off")

row("Indiana Hill drifting ground", "Gold Run (Placer)", "Placer",
    ["indiana hill"], "Gold Run, Placer Co.", 184, 145,
    "Between 1872 and 187 4 the drifting ground ~t Indiana Hill yielded at the rate of $9 to the cubic yard of grave!' i'n place.",
    yd=9.0, basis="production average", years="1872-1874", price_year=1873)

row("Morning Star drift mine (Iowa Hill)", "Iowa Hill (Placer)", "Placer",
    ["morning star"], "Iowa Hill, Placer Co.", 188, 149,
    "The drift mine has proved among the richest. The gravel contained, for a long period, it is stated, $7 a carload, equal to $14 a cubic yard, and the annual production ranged from $25,000 to $150,000.",
    yd=14.0, basis="value-text", years="pre-1911", price_year=1900,
    tonnage="annual production $25,000 to $150,000")

row("Big Dipper (Waterhouse & Dorn) drift mine (Wisconsin Hill)",
    "Iowa Hill (Placer)", "Placer", ["big dipper"], "Iowa Hill, Placer Co.",
    188, 149,
    "A thickness of 5 or 6 feet of gravel is breasted, but the upper 3 feet usually contained but little gold. The yield in 1901 was about 135 carloads of 1 ton each in 24 hours. The average content was $6 a ton. Gravel carrying less than $2 a .ton was not considered to pay.",
    ton=6.0, basis="production average", years="1901", price_year=1901,
    tonnage="about 135 carloads of 1 ton each in 24 hours (1901)")

row("Mayflower drift mine (Forest Hill divide)", "Forest Hill (Placer)",
    "Placer", ["mayflower"], "Forest Hill, Placer Co.", 193, 151,
    "From the Mayflower tunnel, 4, 740 feet long, the main channel has been worked, chiefly from 1888 to 1894, for a distance of 3 miles, connecting it with the Paragon workings. (See Pl. XXV.) A bed of gravel from 2 to 14 feet thick, having an average width of 75 feet; was removed from the bedrock. The yield has been approximately $1,500,000, or $7 to the ton of loose gravel delivered. Two-thirds of the bottom gravel was found to pay for extraction.",
    ton=7.0, basis="production average", years="1888-1894", price_year=1890,
    tonnage="yield approximately $1,500,000")

row("Paragon drift mine (main channel, Bath)", "Forest Hill-Bath (Placer)",
    "Placer", ["paragon"], "Bath, near Forest Hill", 193, 151,
    "The same channel has been worked from the Paragon mine to a distance of 6,800 feet north. The width of gravel breasted is 50 feet, the depth 2 to 7 feet, the yield per ton delivered at the surface $10, and the total yield by hydraulicking $500,000 and by drifting $850,000.",
    ton=10.0, basis="production average", years="pre-1911", price_year=1900,
    tonnage="total yield $500,000 by hydraulicking and $850,000 by drifting")

row("Paragon drift mine (upper lead on rhyolite tuff)", "Forest Hill-Bath (Placer)",
    "Placer", ["paragon"], "Bath, near Forest Hill", 193, 151,
    "At the Paragon there exists an upper streak of pay gravel 150 feet above the bedrock; this was followed for 2,000 feet until cut off by a channel of intervolcanic erosion filled with andesitic tuff. The width of this upper lead was 225 feet, the depth of noncemented pay gravel 5 feet) and the yield per ton of loose gravel $4.50. The total yield was $900,000.",
    ton=4.50, basis="production average", years="pre-1911", price_year=1900,
    tonnage="total yield of upper lead $900,000")

row("Hidden Treasure drift mine (White channel, Sunny South)",
    "Damascus-Sunny South (Placer)", "Placer", ["hidden treasure"],
    "Sunny South, Placer Co.", 194, 152,
    "From Sunny South,. 3! miles farther south, where the bedrock elevation is 3,644 feet, the Hidden Treasure Co. has worked the deposit for 7,700 feet northward. The width of gravel breasted was 250 feet; the depth 4 to 7 feet, including 1 foot of bedrock; the yield of loose gravel delivered from 50 cents to $1.75 a ton. The working costs, which are unusually low, approximate 50 cents a ton.",
    ton=0.50, basis="production average", years="circa 1885-1907", price_year=1900,
    tonnage="total production to 1910 about $3,500,000")

row("Red Point drift mine (value per carload of 22 cu. ft.)",
    "Damascus (Placer)", "Placer", ["red point"], "Damascus, Placer Co.",
    199, 157,
    "The production from January 1, 1884, to December 31, 1892, was 140,345 carloads of 22 cubic feet each, yielding $308,245, or $2.20 a carload. A distance of 5,073 feet yielded at the rate of $71.65 a running foot. The total expense per carload was $1.64.",
    ton=2.20, basis="production average", years="1884-1892", price_year=1888,
    tonnage="140,345 carloads of 22 cubic feet, 1884-1892 ($308,245)")

row("Shady Run channel (Cedar Creek diggings, pay streak)",
    "Blue Canyon-Shady Run (Placer)", "Placer", ["shady run", "cedar creek"],
    "Shady Run station, Placer Co.", 185, 146,
    "It is up to 400 feet wide and contains from 4 to 70 feet of well-washed quartz gravel. The pay streak, as a rule, occupies only a part of the depression and the gravel is reported to run f;rom 50 to 90 cents a ton. Outside of the pay streak it averages 45 cents. The gold is coarse and its fineness is 950.",
    ton=0.50, basis="value-text", years="circa 1896-1910", price_year=1900)

# ---------------- Eldorado County ----------------
row("Excelsior claim (hydraulic, Placerville)", "Placerville (Eldorado)",
    "Eldorado", ["excelsior"], "Placerville, Eldorado Co.", 88, 72,
    "Rich gravels were also mined near Placerville. At the Excelsior claim a considerable mass of gravel 100 feet in thickness is stated on reliable authority to have averaged $1 a cubic yard, worked by the hydraulic method. At this place two upper pay streaks occurred, one 25 feet and the other 60 feet above the bedrock.",
    yd=1.0, basis="value-text", years="1850s-1870s", price_year=1865)

row("Blue lead (deep gravel, Placerville)", "Placerville (Eldorado)",
    "Eldorado", ["blue lead"], "Placerville, Eldorado Co.", 88, 72,
    "The deep gravel of the so-called Blue lead at Placerville, averaging about 100 feet in width, yielded cemented gravel containing from $2 to $3.50 a cubic yard.",
    yd=2.0, basis="production", years="pre-1911", price_year=1900)

row("Coon Hollow diggings (Placerville)", "Placerville (Eldorado)", "Eldorado",
    ["coon hollow"], "Placerville, Eldorado Co.", 214, 172,
    "Those of Coon Hollow were of very much higher grade. The whole mass of gravels mined at Coon Hollow, a thickness of at least 100 feet, is believed by Mr. Alderson to have averaged $1 a cubic yard.",
    yd=1.0, basis="value-text", years="1852-1871", price_year=1865)

row("Linden mine (bench gravel, Placerville)", "Placerville (Eldorado)",
    "Eldorado", ["linden"], "Placerville, Eldorado Co.", 220, 178,
    "The Linden bench gravel was about 5 feet deep. It averaged $2 a carload of 1,800 pounds, or $3.25 a cubic yard, and the total output from 18S2 to 1894 was $130,000.",
    yd=3.25, basis="production average", years="1882-1894", price_year=1888,
    tonnage="total output 1882-1894 $130,000")

# ---------------- Calaveras County ----------------
row("North Star mine (Deep Blue lead, Mokelumne Hill)",
    "Mokelumne Hill (Calaveras)", "Calaveras", ["north star"],
    "Mokelumne Hill, Calaveras Co.", 88, 72,
    "The gravel in the Deep Blue lead of Mokelumne Hill, at the North Star mine, is said to average $1.95 a ton in drifting operations.",
    ton=1.95, basis="value-text", years="pre-1911", price_year=1900,
    excl=["grass valley"])

row("Banner Blue Gravel mine (Fort Mountain channel)",
    "Railroad Flat (Calaveras)", "Calaveras", ["banner"],
    "Railroad Flat, Calaveras Co.", 255, 211,
    "the Banner Blue Gravel mine, on Jesus Maria Creek, worked by a shaft sunk 63 feet deep through the rhyolite. The gravel is about 100 feet wide, carries from 25 to 40 per cent of coarse bowlders, and contains much black sand and iron sulphide. A thickness of 6 to 8 feet of gravel is exhibited, which is said to contain from $3 to $4 a ton and is mined and milled for $1 to $1.25 a ton.",
    ton=3.0, basis="value-text", years="pre-1911", price_year=1898)

row("Lava Bed mine channel (near Sheep Ranch)", "Sheep Ranch (Calaveras)",
    "Calaveras", ["lava bed"], "Sheep Ranch, Calaveras Co.", 255, 211,
    "Drifting operations have also been carried on at the Lava Bed mine through a shaft 85 feet in depth; the partly cemented high-grade gravel has been followed for several hundred feet and is crushed and amalgamated in a 5-stamp mill. At least a mile of the rhyolitecapped channel remained unworked in 1902. The channel is thought to be 150 to 200 feet wide and covered by 24 feet of gravel and sand. l'he gravel is coarse, subangular, and poorly assorted. Tests are said to have given an average value for the entire thickness of 50 cents a cubic yard.",
    yd=0.50, basis="assay", years="circa 1902", price_year=1902)

row("Jackrabbit ground (Central Hill channel)", "San Andreas (Calaveras)",
    "Calaveras", ["jackrabbit", "jack rabbit"], "Dogtown, near San Andreas",
    246, 203,
    "The gravels have been opened here by a shaft 191 feet in depth with a 100-foot drift in gravel to the south from its bottom and a lower tunnel running 1,200 feet in bedrock. The gravel has been prospected a distance of 300 feet along the course of the channel and breasted for a distance of 75 feet to a width of 35 feet and height of 7 feet; the gravel extracted is said to have contained from $2 to $10 a cubic yard.",
    yd=2.0, basis="value-text", years="pre-1911", price_year=1905)

row("Monarch ground (Central Hill channel)", "San Andreas (Calaveras)",
    "Calaveras", ["monarch"], "Dogtown, near San Andreas", 246, 203,
    "The Monarch pit shows that the rim is overlain by 25 feet of prevolcanic gravel, covered by volcanic material. At this point the channel has been explored through a 500-foot tunnel; some of the gravel is stated to have averaged $5 a ton.",
    ton=5.0, basis="value-text", years="pre-1911", price_year=1905)

row("Rose Hill property (Emery pit, Eldorado Gulch)",
    "Mountain Ranch (Calaveras)", "Calaveras", ["rose hill", "emery"],
    "Mountain Ranch, Calaveras Co.", 255, 211,
    "It is probable that this stream was a tributary to the main channel. The recovery averaged 37 cents to the yard during the first",
    yd=0.37, basis="production average", years="1901-1902", price_year=1901)

# ---------------- Sacramento County ----------------
row("Natomas Consolidated dredging ground (Folsom)", "Folsom (Sacramento)",
    "Sacramento", ["natomas", "folsom"], "Folsom, Sacramento Co.", 268, 222,
    "In 1909 the Natomas Consolidated Co. turned over 321.48 acres and handled 13,975,185 cubic yards of gravel at a cost of 3.6 cents a cubic yard while digging to an average depth of 27 feet on ground ranging from 19 to 70 feet in depth. The values vary from 6 to 18 cents a cubic yard.",
    yd=0.06, basis="production", years="1909", price_year=1909,
    tonnage="13,975,185 cubic yards handled in 1909")

out = '/home/claude/nw/grades-research/agent_out/rows_pp73.json'
json.dump(R, open(out, 'w'), indent=1)
print(f'wrote {len(R)} rows -> {out}')

# quick self-check of quote scores
pt = G.load_pagetext('pp73')
for r in R:
    s = G.quote_on_page(r['quote'], pt['pages'][r['pdf_page'] - 1])
    flag = '' if s >= 0.9 else ('  <-- LOW' if s >= 0.85 else '  <-- FAIL')
    print(f"{s:0.3f} {r['name'][:58]}{flag}")
