"""geomodel.composition — what the text says is in the ground, read off a
lexicon and placed at the depth the sentence names.  Never invented.

A mine description says what the workings found as often as where they went:
"on the 300 level the vein carries galena, sphalerite and a little
chalcopyrite in a gangue of quartz and calcite; above the 100 level the ore
is oxidized, with cerussite and limonite".  This module reads those
sentences with a fixed lexicon of ore, gangue, alteration and host-rock
names, ties each statement to the level (and the working) its sentence
names, and hands the builder one point per level-tied statement so the
model shows not just where the mine went but what the text says it found
there.

Three rules, the same ones :mod:`narrative` and :mod:`assay` obey:

* **Nothing is inferred beyond the lexicon.**  Galena implies lead because
  the lexicon says so; the word "ore" implies nothing; a mineral that is not
  in the lexicon is not read.  A commodity is implied only by a named ore
  mineral — quartz and calcite are gangue and imply no metal.
* **Every entry carries the sentence it came from** with that sentence's
  character span in the input text, plus the exact span of the word as
  written, so ``text[at[0]:at[1]] == as_written``.
* **Depth is the sentence's, not the modeller's.**  A statement is tied to a
  level only when its sentence (or the clauses before it in the same
  period-sentence) names one, using the level grammar :mod:`narrative`
  uses.  A statement that names no level and no working produces no point;
  it stays in the manifest block as text.

Bare metal words ("gold", "silver", "copper") are deliberately *not*
minerals here: "20 ounces silver" is an assay (:mod:`assay` reads it) and
"Gold Hill" is a name.  Only the mineral phrases — native gold, free gold,
wire silver, native copper — count as an occurrence.
"""
import hashlib
import json
import re

from . import narrative
from .model import PointSet

COMPOSITION_VERSION = 'nwmm-composition/1'

# ---------------------------------------------------------------- lexicon
#: (canonical name, role, commodities implied, surface forms as written).
#: role is one of ore / gangue / alteration / host.  Commodities are a tuple
#: because "argentiferous galena" states two metals; the first is the
#: entry's principal commodity.  Historic synonyms and formulas are listed
#: so a 1910 bulletin's "zinc blende" and a 1950 one's "sphalerite" are the
#: same mineral in the model.
MINERALS = [
    # --- precious metals
    ('native gold', 'ore', ('gold',),
     ('native gold', 'free gold', 'free-milling gold', 'free milling gold', 'visible gold',
      'wire gold', 'leaf gold', 'coarse gold', 'fine gold', 'flour gold', 'gold dust',
      'placer gold', 'nugget gold')),
    ('electrum', 'ore', ('gold', 'silver'), ('electrum',)),
    ('native silver', 'ore', ('silver',),
     ('native silver', 'wire silver', 'leaf silver', 'free silver', 'metallic silver')),
    ('argentite', 'ore', ('silver',),
     ('argentite', 'acanthite', 'silver glance', 'silver sulphide', 'silver sulfide')),
    ('cerargyrite', 'ore', ('silver',),
     ('cerargyrite', 'chlorargyrite', 'horn silver', 'horn-silver', 'silver chloride',
      'chloride of silver', 'embolite', 'bromyrite')),
    ('pyrargyrite', 'ore', ('silver',), ('pyrargyrite', 'ruby silver', 'dark ruby silver')),
    ('proustite', 'ore', ('silver',), ('proustite', 'light ruby silver')),
    ('polybasite', 'ore', ('silver',), ('polybasite',)),
    ('stephanite', 'ore', ('silver',), ('stephanite', 'brittle silver')),
    ('hessite', 'ore', ('silver',), ('hessite', 'silver telluride')),
    ('freibergite', 'ore', ('silver', 'copper'), ('freibergite', 'argentiferous tetrahedrite')),
    ('sylvanite', 'ore', ('gold', 'silver'), ('sylvanite',)),
    ('calaverite', 'ore', ('gold',), ('calaverite',)),
    ('petzite', 'ore', ('gold', 'silver'), ('petzite',)),
    ('telluride', 'ore', ('gold',), ('telluride', 'gold telluride', 'tellurides of gold')),
    ('native platinum', 'ore', ('platinum',), ('native platinum',)),
    # --- lead
    ('galena', 'ore', ('lead',),
     ('galena', 'galenite', 'lead sulphide', 'lead sulfide', 'lead glance')),
    ('argentiferous galena', 'ore', ('lead', 'silver'),
     ('argentiferous galena', 'silver-bearing galena', 'silver bearing galena')),
    ('cerussite', 'ore', ('lead',),
     ('cerussite', 'lead carbonate', 'carbonate of lead', 'white lead ore')),
    ('anglesite', 'ore', ('lead',), ('anglesite', 'lead sulphate', 'lead sulfate')),
    ('pyromorphite', 'ore', ('lead',), ('pyromorphite', 'green lead ore')),
    ('wulfenite', 'ore', ('lead', 'molybdenum'), ('wulfenite', 'lead molybdate')),
    ('plumbojarosite', 'ore', ('lead',), ('plumbojarosite',)),
    # --- zinc
    ('sphalerite', 'ore', ('zinc',),
     ('sphalerite', 'zinc blende', 'zincblende', 'blende', 'black jack', 'blackjack',
      'rosin jack', 'ruby jack', 'zinc sulphide', 'zinc sulfide')),
    ('smithsonite', 'ore', ('zinc',),
     ('smithsonite', 'zinc carbonate', 'carbonate of zinc', 'dry-bone ore', 'dry bone ore',
      'dry bone', 'turkey-fat ore')),
    ('hemimorphite', 'ore', ('zinc',), ('hemimorphite', 'calamine', 'zinc silicate')),
    ('willemite', 'ore', ('zinc',), ('willemite',)),
    ('zincite', 'ore', ('zinc',), ('zincite',)),
    ('franklinite', 'ore', ('zinc', 'iron'), ('franklinite',)),
    # --- copper
    ('chalcopyrite', 'ore', ('copper',),
     ('chalcopyrite', 'copper pyrites', 'copper pyrite', 'yellow copper ore', 'yellow copper')),
    ('bornite', 'ore', ('copper',),
     ('bornite', 'peacock ore', 'peacock copper', 'purple copper ore', 'erubescite',
      'horseflesh ore')),
    ('chalcocite', 'ore', ('copper',), ('chalcocite', 'copper glance', 'chalcosine')),
    ('covellite', 'ore', ('copper',), ('covellite', 'indigo copper')),
    ('tetrahedrite', 'ore', ('copper',),
     ('tetrahedrite', 'gray copper', 'grey copper', 'gray copper ore', 'grey copper ore',
      'fahlore', 'fahl ore')),
    ('tennantite', 'ore', ('copper',), ('tennantite',)),
    ('enargite', 'ore', ('copper',), ('enargite',)),
    ('malachite', 'ore', ('copper',), ('malachite', 'green copper carbonate', 'green carbonate')),
    ('azurite', 'ore', ('copper',), ('azurite', 'blue copper carbonate', 'chessylite', 'blue carbonate')),
    ('copper carbonates', 'ore', ('copper',),
     ('copper carbonate', 'copper carbonates', 'carbonates of copper', 'carbonate of copper',
      'copper stain', 'copper stains', 'copper staining')),
    ('chrysocolla', 'ore', ('copper',), ('chrysocolla', 'copper silicate')),
    ('cuprite', 'ore', ('copper',),
     ('cuprite', 'red copper oxide', 'red oxide of copper', 'copper oxide', 'red copper ore')),
    ('native copper', 'ore', ('copper',), ('native copper', 'metallic copper')),
    ('tenorite', 'ore', ('copper',), ('tenorite', 'melaconite', 'copper pitch')),
    ('brochantite', 'ore', ('copper',), ('brochantite',)),
    ('chalcanthite', 'ore', ('copper',),
     ('chalcanthite', 'copper sulphate', 'copper sulfate', 'blue vitriol', 'bluestone')),
    ('antlerite', 'ore', ('copper',), ('antlerite',)),
    ('atacamite', 'ore', ('copper',), ('atacamite',)),
    # --- antimony, mercury, arsenic, bismuth
    ('stibnite', 'ore', ('antimony',),
     ('stibnite', 'antimonite', 'antimony sulphide', 'antimony sulfide', 'antimony glance',
      'gray antimony', 'grey antimony')),
    ('jamesonite', 'ore', ('antimony', 'lead'), ('jamesonite', 'feather ore')),
    ('boulangerite', 'ore', ('antimony', 'lead'), ('boulangerite',)),
    ('cinnabar', 'ore', ('mercury',),
     ('cinnabar', 'quicksilver ore', 'mercury sulphide', 'mercury sulfide', 'vermilion')),
    ('native mercury', 'ore', ('mercury',),
     ('native mercury', 'native quicksilver', 'free mercury', 'free quicksilver',
      'metallic mercury')),
    ('arsenopyrite', 'ore', ('arsenic',),
     ('arsenopyrite', 'mispickel', 'arsenical pyrite', 'arsenical pyrites', 'arsenical iron')),
    ('realgar', 'ore', ('arsenic',), ('realgar',)),
    ('orpiment', 'ore', ('arsenic',), ('orpiment',)),
    ('bismuthinite', 'ore', ('bismuth',), ('bismuthinite', 'bismuth glance', 'bismuthite')),
    ('native bismuth', 'ore', ('bismuth',), ('native bismuth',)),
    # --- tungsten, molybdenum, tin
    ('scheelite', 'ore', ('tungsten',), ('scheelite', 'calcium tungstate')),
    ('wolframite', 'ore', ('tungsten',), ('wolframite', 'wolfram')),
    ('huebnerite', 'ore', ('tungsten',), ('huebnerite', 'hübnerite', 'hubnerite')),
    ('ferberite', 'ore', ('tungsten',), ('ferberite',)),
    ('molybdenite', 'ore', ('molybdenum',),
     ('molybdenite', 'molybdenum sulphide', 'molybdenum sulfide')),
    ('cassiterite', 'ore', ('tin',), ('cassiterite', 'tinstone', 'tin stone', 'stream tin', 'wood tin')),
    ('stannite', 'ore', ('tin',), ('stannite', 'tin pyrites')),
    # --- iron sulphides and oxides
    ('pyrite', 'gangue', (),
     ('pyrite', 'iron pyrites', 'iron pyrite', 'pyrites', 'mundic', "fool's gold", 'fools gold')),
    ('pyrrhotite', 'gangue', (), ('pyrrhotite', 'magnetic pyrites', 'magnetic pyrite')),
    ('marcasite', 'gangue', (), ('marcasite', 'white iron pyrites')),
    ('magnetite', 'ore', ('iron',), ('magnetite', 'magnetic iron ore', 'lodestone', 'loadstone')),
    ('hematite', 'ore', ('iron',),
     ('hematite', 'haematite', 'specularite', 'specular hematite', 'specular iron',
      'red iron ore', 'red ochre', 'red ocher')),
    ('limonite', 'alteration', (),
     ('limonite', 'brown iron ore', 'bog iron', 'yellow ochre', 'yellow ocher', 'iron oxide',
      'iron oxides', 'oxides of iron', 'hydrated iron oxide', 'hydrous iron oxide')),
    ('goethite', 'alteration', (), ('goethite',)),
    ('jarosite', 'alteration', (), ('jarosite',)),
    ('siderite', 'gangue', (), ('siderite', 'spathic iron', 'iron carbonate', 'chalybite')),
    ('ilmenite', 'ore', ('titanium',), ('ilmenite', 'titanic iron')),
    ('rutile', 'ore', ('titanium',), ('rutile',)),
    ('chromite', 'ore', ('chromium',), ('chromite', 'chrome iron ore', 'chrome ore')),
    # --- manganese
    ('rhodochrosite', 'gangue', (), ('rhodochrosite', 'manganese carbonate', 'manganese spar')),
    ('rhodonite', 'gangue', (), ('rhodonite', 'manganese silicate')),
    ('manganese oxides', 'ore', ('manganese',),
     ('manganese oxide', 'manganese oxides', 'oxides of manganese', 'oxide of manganese',
      'black oxide of manganese', 'manganese stain', 'manganese stains', 'manganese staining')),
    ('psilomelane', 'ore', ('manganese',), ('psilomelane',)),
    ('pyrolusite', 'ore', ('manganese',), ('pyrolusite',)),
    ('wad', 'ore', ('manganese',), ('wad', 'bog manganese')),
    # --- uranium, vanadium, lithium, beryllium, rare metals
    ('uraninite', 'ore', ('uranium',), ('uraninite', 'pitchblende')),
    ('carnotite', 'ore', ('uranium', 'vanadium'), ('carnotite',)),
    ('autunite', 'ore', ('uranium',), ('autunite',)),
    ('torbernite', 'ore', ('uranium', 'copper'), ('torbernite',)),
    ('vanadinite', 'ore', ('vanadium', 'lead'), ('vanadinite',)),
    ('roscoelite', 'ore', ('vanadium',), ('roscoelite', 'vanadium mica')),
    ('beryl', 'ore', ('beryllium',), ('beryl', 'aquamarine')),
    ('spodumene', 'ore', ('lithium',), ('spodumene',)),
    ('lepidolite', 'ore', ('lithium',), ('lepidolite', 'lithia mica')),
    ('amblygonite', 'ore', ('lithium',), ('amblygonite',)),
    ('columbite', 'ore', ('niobium', 'tantalum'), ('columbite', 'niobite')),
    ('tantalite', 'ore', ('tantalum', 'niobium'), ('tantalite',)),
    ('monazite', 'ore', ('rare earths',), ('monazite',)),
    ('zircon', 'gangue', (), ('zircon',)),
    ('cobaltite', 'ore', ('cobalt',), ('cobaltite',)),
    ('erythrite', 'ore', ('cobalt',), ('erythrite', 'cobalt bloom')),
    ('pentlandite', 'ore', ('nickel',), ('pentlandite',)),
    ('garnierite', 'ore', ('nickel',), ('garnierite',)),
    ('millerite', 'ore', ('nickel',), ('millerite',)),
    ('niccolite', 'ore', ('nickel',), ('niccolite', 'nickeline')),
    ('native sulphur', 'ore', ('sulphur',), ('native sulphur', 'native sulfur')),
    # --- gangue and vein minerals
    ('quartz', 'gangue', (),
     ('quartz', 'vein quartz', 'white quartz', 'milky quartz', 'glassy quartz', 'comb quartz',
      'sugary quartz', 'drusy quartz', 'bull quartz', 'ribbon quartz', 'honeycomb quartz',
      'rose quartz', 'smoky quartz', 'amethyst')),
    ('chalcedony', 'gangue', (), ('chalcedony', 'chalcedonic quartz', 'chalcedonic silica')),
    ('opal', 'gangue', (), ('opal', 'opaline', 'opaline silica', 'hyalite')),
    ('jasper', 'gangue', (), ('jasper', 'jasperoid')),
    ('adularia', 'gangue', (), ('adularia', 'valencianite')),
    ('calcite', 'gangue', (),
     ('calcite', 'calc-spar', 'calc spar', 'calcspar', 'calcium carbonate', 'lime carbonate',
      'crystalline calcite', 'iceland spar')),
    ('dolomite', 'gangue', (), ('dolomite', 'pearl spar')),
    ('ankerite', 'gangue', (), ('ankerite',)),
    ('fluorite', 'ore', ('fluorspar',), ('fluorite', 'fluorspar', 'fluor spar', 'fluor-spar', 'fluor')),
    ('barite', 'ore', ('barite',),
     ('barite', 'barytes', 'baryte', 'heavy spar', 'heavy-spar', 'heavyspar', 'barium sulphate',
      'barium sulfate')),
    ('celestite', 'gangue', (), ('celestite', 'celestine')),
    ('gypsum', 'ore', ('gypsum',), ('gypsum', 'selenite', 'satin spar', 'alabaster')),
    ('anhydrite', 'gangue', (), ('anhydrite',)),
    ('alunite', 'alteration', (), ('alunite',)),
    ('kaolin', 'alteration', (), ('kaolin', 'kaolinite', 'china clay')),
    ('clay', 'alteration', (), ('clay', 'clays', 'clayey material', 'clay seam', 'clay seams')),
    ('sericite', 'alteration', (), ('sericite', 'sericitic mica')),
    ('chlorite', 'alteration', (), ('chlorite',)),
    ('epidote', 'alteration', (), ('epidote',)),
    ('tourmaline', 'gangue', (), ('tourmaline', 'schorl')),
    ('apatite', 'gangue', (), ('apatite',)),
    ('garnet', 'gangue', (), ('garnet', 'grossularite', 'grossular', 'andradite')),
    ('wollastonite', 'gangue', (), ('wollastonite',)),
    ('diopside', 'gangue', (), ('diopside',)),
    ('actinolite', 'gangue', (), ('actinolite',)),
    ('tremolite', 'gangue', (), ('tremolite',)),
    ('talc', 'ore', ('talc',), ('talc', 'soapstone', 'steatite')),
    ('serpentine', 'host', (), ('serpentine',)),
    ('asbestos', 'ore', ('asbestos',), ('asbestos', 'chrysotile', 'amphibole asbestos')),
    ('graphite', 'ore', ('graphite',), ('graphite', 'plumbago', 'black lead')),
    ('mica', 'gangue', (), ('mica', 'micas')),
    ('muscovite', 'gangue', (), ('muscovite', 'white mica', 'isinglass')),
    ('biotite', 'host', (), ('biotite', 'black mica')),
    ('feldspar', 'gangue', (), ('feldspar', 'felspar', 'feldspars', 'felspars')),
    ('orthoclase', 'host', (), ('orthoclase', 'microcline')),
    ('plagioclase', 'host', (), ('plagioclase', 'albite', 'oligoclase', 'andesine', 'labradorite')),
    ('hornblende', 'host', (), ('hornblende',)),
    ('augite', 'host', (), ('augite',)),
    ('olivine', 'host', (), ('olivine',)),
    ('zeolite', 'gangue', (), ('zeolite', 'zeolites', 'stilbite', 'heulandite', 'laumontite')),
    ('halite', 'ore', ('salt',), ('halite', 'rock salt', 'common salt')),
    ('colemanite', 'ore', ('borates',), ('colemanite', 'ulexite', 'borax')),
    ('magnesite', 'ore', ('magnesite',), ('magnesite',)),
    ('corundum', 'ore', ('corundum',), ('corundum', 'emery')),
]

#: host rocks: (canonical, surface forms).  The rock the vein is in.
HOST_ROCKS = [
    ('granite', ('granite', 'granites', 'granitic rock', 'granitic rocks')),
    ('granodiorite', ('granodiorite',)),
    ('quartz monzonite', ('quartz monzonite', 'quartz-monzonite')),
    ('monzonite', ('monzonite',)),
    ('diorite', ('diorite',)),
    ('quartz diorite', ('quartz diorite', 'quartz-diorite')),
    ('gabbro', ('gabbro',)),
    ('diabase', ('diabase',)),
    ('syenite', ('syenite',)),
    ('porphyry', ('porphyry', 'porphyries', 'porphyritic rock', 'quartz porphyry', 'granite porphyry',
                  'monzonite porphyry', 'diorite porphyry', 'rhyolite porphyry', 'felsite porphyry',
                  'andesite porphyry', 'quartz-porphyry')),
    ('rhyolite', ('rhyolite', 'rhyolites')),
    ('andesite', ('andesite', 'andesites')),
    ('dacite', ('dacite',)),
    ('latite', ('latite', 'quartz latite', 'quartz-latite')),
    ('trachyte', ('trachyte',)),
    ('basalt', ('basalt', 'basalts')),
    ('felsite', ('felsite',)),
    ('tuff', ('tuff', 'tuffs', 'volcanic tuff', 'rhyolite tuff', 'welded tuff', 'tuffaceous')),
    ('agglomerate', ('agglomerate', 'volcanic agglomerate')),
    ('lamprophyre', ('lamprophyre',)),
    ('aplite', ('aplite',)),
    ('pegmatite', ('pegmatite', 'pegmatites', 'pegmatite dike', 'pegmatite dikes')),
    ('greenstone', ('greenstone', 'greenstones')),
    ('limestone', ('limestone', 'limestones', 'dolomitic limestone', 'blue limestone', 'gray limestone',
                   'grey limestone', 'black limestone', 'crystalline limestone')),
    ('marble', ('marble',)),
    ('shale', ('shale', 'shales', 'black shale')),
    ('slate', ('slate', 'slates')),
    ('argillite', ('argillite',)),
    ('quartzite', ('quartzite', 'quartzites')),
    ('sandstone', ('sandstone', 'sandstones')),
    ('conglomerate', ('conglomerate', 'conglomerates')),
    ('siltstone', ('siltstone',)),
    ('mudstone', ('mudstone',)),
    ('graywacke', ('graywacke', 'greywacke')),
    ('schist', ('schist', 'schists', 'mica schist', 'chlorite schist', 'hornblende schist',
                'sericite schist', 'quartz schist', 'talc schist', 'mica-schist')),
    ('gneiss', ('gneiss', 'gneisses', 'granite gneiss')),
    ('phyllite', ('phyllite',)),
    ('hornfels', ('hornfels',)),
    ('chert', ('chert', 'cherts', 'cherty')),
    ('serpentinite', ('serpentinite', 'serpentine rock')),
    ('amphibolite', ('amphibolite',)),
    ('peridotite', ('peridotite',)),
    ('dunite', ('dunite',)),
    ('pyroxenite', ('pyroxenite',)),
]

#: alteration and zone terms: (canonical, zone hint, surface forms)
ALTERATION = [
    ('silicified', None, ('silicified', 'silicification', 'silicic', 'silicified zone')),
    ('sericitized', None, ('sericitized', 'sericitised', 'sericitization', 'sericitic')),
    ('kaolinized', None, ('kaolinized', 'kaolinised', 'kaolinization')),
    ('propylitic', None, ('propylitic', 'propylitized', 'propylitization', 'propylite')),
    ('argillic', None, ('argillic', 'argillized', 'argillization')),
    ('oxidized', 'oxidized', ('oxidized', 'oxidised', 'oxidation', 'oxide zone', 'zone of oxidation',
                              'oxidized zone', 'oxidised zone', 'oxide ore', 'oxide ores',
                              'oxidized ore', 'oxidized ores')),
    ('sulphide', 'sulphide', ('sulphide', 'sulfide', 'sulphides', 'sulfides', 'sulphide zone',
                              'sulfide zone', 'zone of sulphides', 'zone of sulfides', 'sulphide ore',
                              'sulfide ore', 'sulphide ores', 'sulfide ores', 'primary sulphides',
                              'primary sulfides', 'sulphuret', 'sulphurets')),
    ('gossan', 'oxidized', ('gossan', 'gossans', 'iron hat', 'iron cap', 'iron capping')),
    ('skarn', None, ('skarn', 'tactite', 'garnetized', 'garnetite')),
    ('contact metamorphic', None, ('contact metamorphic', 'contact-metamorphic', 'contact metamorphism',
                                   'contact zone', 'contact deposit', 'contact deposits')),
    ('chloritized', None, ('chloritized', 'chloritised', 'chloritization', 'chloritic')),
    ('pyritized', None, ('pyritized', 'pyritised', 'pyritization', 'pyritic')),
    ('bleached', None, ('bleached', 'bleaching')),
    ('leached', None, ('leached', 'leaching')),
    ('iron-stained', None, ('iron-stained', 'iron stained', 'iron staining', 'rusty')),
    ('altered', None, ('altered', 'hydrothermally altered', 'hydrothermal alteration')),
    ('secondary enrichment', None, ('secondary enrichment', 'enriched zone', 'zone of enrichment',
                                    'supergene', 'secondary sulphides', 'secondary sulfides')),
    ('mineralized', None, ('mineralized', 'mineralised', 'mineralization', 'mineralisation')),
]

#: structural ore terms: (canonical, surface forms).  Where the ore sits, in
#: the words the text used.  None of these implies a commodity.
ORE_TERMS = [
    ('ore shoot', ('ore shoot', 'oreshoot', 'ore-shoot', 'shoot', 'pay shoot', 'ore shoots')),
    ('chimney', ('chimney', 'ore chimney', 'ore pipe')),
    ('bonanza', ('bonanza', 'bonanza ore')),
    ('pay streak', ('pay streak', 'paystreak', 'pay-streak', 'streak', 'pay dirt')),
    ('vein filling', ('vein filling', 'vein-filling', 'vein matter', 'vein material', 'vein stuff',
                      'vein-matter')),
    ('breccia', ('breccia', 'brecciated', 'breccia zone', 'breccia ore', 'crushed zone')),
    ('replacement', ('replacement', 'replacement deposit', 'replacement ore', 'replacement body',
                     'bedded replacement', 'replaced')),
    ('ore body', ('ore body', 'orebody', 'ore bodies', 'orebodies')),
    ('lens', ('lens', 'lenticular', 'kidney', 'kidneys')),
    ('pocket', ('pocket', 'pockety')),
    ('stringer', ('stringer', 'stringer zone', 'stringer lode')),
    ('gouge', ('gouge', 'clay gouge', 'gouge seam', 'selvage', 'selvedge')),
    ('banded ore', ('banded', 'crustified', 'banded ore', 'ribbon ore')),
    ('vug', ('vug', 'vuggy', 'druse', 'drusy', 'cavities', 'cavity')),
    ('disseminated', ('disseminated', 'dissemination', 'disseminations')),
    ('fissure vein', ('fissure vein', 'fissure', 'fissure veins')),
    ('bedded deposit', ('bedded deposit', 'bedded vein', 'blanket vein', 'blanket deposit', 'flat vein')),
    ('stockwork', ('stockwork',)),
    ('shear zone', ('shear zone', 'fault zone', 'sheeted zone', 'sheared zone')),
    ('footwall', ('footwall', 'foot wall', 'foot-wall')),
    ('hanging wall', ('hanging wall', 'hangingwall', 'hanging-wall')),
]


def _index():
    """surface form -> (group, canonical, role, commodities, zone)."""
    table = {}
    for name, role, commodities, surfaces in MINERALS:
        for s in surfaces:
            table[_key(s)] = ('minerals', name, role, tuple(commodities), None)
    for name, surfaces in HOST_ROCKS:
        for s in surfaces:
            table.setdefault(_key(s), ('host_rock', name, 'host', (), None))
    for name, zone, surfaces in ALTERATION:
        for s in surfaces:
            table.setdefault(_key(s), ('alteration', name, 'alteration', (), zone))
    for name, surfaces in ORE_TERMS:
        for s in surfaces:
            table.setdefault(_key(s), ('ore_terms', name, None, (), None))
    return table


def _key(surface):
    return re.sub(r'[\s-]+', ' ', surface.lower()).strip()


LEXICON = _index()

#: one scan, longest surface form first, so "quartz monzonite" is a host rock
#: and not the gangue mineral "quartz" followed by a word; an optional plural
#: so "sulphides" and "ore shoots" read.
RE_TERM = re.compile(
    r'\b(?:' + '|'.join(re.escape(s).replace(r'\ ', r'[\s-]+').replace(r'\-', r'[\s-]+')
                        for s in sorted(LEXICON, key=lambda s: (-len(s), s)))
    + r')(?:e?s)?\b', re.I)

#: the two zones a description names by their words alone
RE_ZONE = re.compile(
    r'\b(?P<ox>oxidi[sz]ed|oxidi[sz]ation|oxide[sd]?\s+(?:zone|ores?)|zone\s+of\s+oxidation|gossan)'
    r'|(?P<su>sulphide[sd]?|sulfide[sd]?|sulphurets?|zone\s+of\s+sulphides|zone\s+of\s+sulfides)', re.I)

#: kinds that begin where their level meets the shaft (agentbuild rule 3)
LEVEL_KINDS = ('drift', 'crosscut', 'winze', 'raise', 'stope')


# ---------------------------------------------------------------- reading
def _lookup(surface):
    key = _key(surface)
    for cand in (key, key[:-1] if key.endswith('s') else None, key[:-2] if key.endswith('es') else None):
        if cand and cand in LEXICON:
            return LEXICON[cand]
    return None


def _levels_in(body, off):
    """[(label, abs_start, abs_end)] in ``body`` using narrative's grammar and
    precedence: "No. 3 level" is not also a "3 level"."""
    out, taken = [], []
    for pattern, label in ((narrative.RE_LEVEL_NO, lambda m: 'No. %s' % m.group('n')),
                           (narrative.RE_LEVEL_NUM, lambda m: m.group('lv').replace(',', '')),
                           (narrative.RE_LEVEL_NAMED,
                            lambda m: re.sub(r'\s+', ' ', m.group('n').lower()))):
        for m in pattern.finditer(body):
            if any(m.start() < b and a < m.end() for a, b in taken):
                continue
            taken.append((m.start(), m.end()))
            out.append((label(m), off + m.start(), off + m.end()))
    out.sort(key=lambda t: t[1])
    return out


def _nearest_level(levels, at):
    """The level named nearest before ``at``, else the first after it."""
    before = [lv for lv in levels if lv[2] <= at]
    if before:
        return before[-1]
    after = [lv for lv in levels if lv[1] >= at]
    return after[0] if after else None


def _zone_at(body, at):
    """The zone word nearest before the offset in the sentence, else after."""
    hits = [(m.start(), m.end(), 'oxidized' if m.group('ox') else 'sulphide')
            for m in RE_ZONE.finditer(body)]
    before = [h for h in hits if h[1] <= at]
    if before:
        return before[-1][2]
    after = [h for h in hits if h[0] >= at]
    return after[0][2] if after else None


def _clauses(sents, text):
    """Group index -> the sentences() entries that share one period-sentence:
    a clause split off by ';' or ':' keeps the clauses before it as its
    window, so "on the 300 level ... ; the gangue is quartz" can tie the
    second clause to the level the first one named."""
    groups, group = [], []
    for s, e, body in sents:
        group.append((s, e, body))
        terminator = text[e - 1:e]
        if terminator not in (';', ':'):
            groups.append(group)
            group = []
    if group:
        groups.append(group)
    return groups


def _element_for(spec, span, level):
    """The spec element read from this sentence (span overlap), preferring
    one on the statement's level when several share the sentence."""
    cands = []
    for el in (spec or {}).get('elements') or []:
        sp = el.get('span')
        if not sp:
            continue
        if sp[0] < span[1] and span[0] < sp[1]:
            cands.append(el)
    if not cands:
        return None
    if level:
        same = [el for el in cands if el.get('level') == level]
        if same:
            cands = same
    cands.sort(key=lambda el: (el['span'][0], _elnum(el['id'])))
    return cands[0]['id']


def _elnum(eid):
    m = re.search(r'\d+', eid or '')
    return int(m.group(0)) if m else 0


def compose(text, spec=None):
    """What the text names underground, sentence by sentence.

    Returns ``{'minerals', 'host_rock', 'alteration', 'ore_terms',
    'statements', 'commodities', 'by_level', 'coverage', ...}``.  Every entry
    carries ``quote`` and ``span`` (the sentence, as narrative does) and
    ``at`` (the word itself), so both index back into ``text`` exactly.
    Deterministic; the same text always gives the same dict.
    """
    text = text or ''
    sents = narrative.sentences(text)
    page = (spec or {}).get('page')
    groups = {'minerals': [], 'host_rock': [], 'alteration': [], 'ore_terms': []}
    statements = []
    commodities, by_level = {}, {}
    seen_statements = 0

    for clause in _clauses(sents, text):
        window_levels = []                       # levels named by earlier clauses
        for start, end, body in clause:
            quote = re.sub(r'\s+', ' ', body).strip()
            span = [start, end]
            hits = []
            for m in RE_TERM.finditer(body):
                entry = _lookup(m.group(0))
                if entry is None:
                    continue                      # a plural the table does not know
                hits.append((m.start(), m.end(), entry))
            levels_here = _levels_in(body, start)
            level = level_source = None
            if hits:
                first_at = start + hits[0][0]
                got = _nearest_level(levels_here, first_at)
                if got:
                    level, level_source = got[0], 'sentence'
                elif window_levels:
                    level, level_source = window_levels[-1][0], 'window'
            element = _element_for(spec, span, level) if hits else None
            sid = None
            minerals_here, as_written_here, zones_here = [], [], []
            for a, b, (group, name, role, comms, zone_hint) in hits:
                zone = _zone_at(body, a) if group in ('minerals', 'alteration') else None
                if group == 'alteration' and zone is None:
                    zone = zone_hint
                rec = {'name': name, 'as_written': body[a:b], 'at': [start + a, start + b],
                       'quote': quote, 'span': span, 'level': level, 'element': element}
                if group == 'minerals':
                    if sid is None:
                        seen_statements += 1
                        sid = 'c%d' % seen_statements
                    rec.update({'role': role, 'commodity': comms[0] if comms else None,
                                'commodities': list(comms), 'zone': zone, 'statement': sid})
                    minerals_here.append(name)
                    as_written_here.append(body[a:b])
                    if zone and zone not in zones_here:
                        zones_here.append(zone)
                    for c in comms:
                        slot = commodities.setdefault(c, {'commodity': c, 'count': 0, 'minerals': []})
                        slot['count'] += 1
                        if name not in slot['minerals']:
                            slot['minerals'].append(name)
                    if level:
                        names = by_level.setdefault(level, [])
                        if name not in names:
                            names.append(name)
                elif group == 'alteration':
                    rec['zone'] = zone
                groups[group].append(rec)
            if sid is not None:
                statements.append({
                    'id': sid, 'quote': quote, 'span': span,
                    'minerals': list(dict.fromkeys(minerals_here)),
                    'as_written': as_written_here,
                    'zone': '/'.join(zones_here) if zones_here else None,
                    'level': level, 'level_source': level_source,
                    'element': element, 'page': page,
                })
            # host, alteration and ore-term entries in a mineral-less sentence
            # belong to no statement; the ones in a mineral sentence do
            for group in ('host_rock', 'alteration', 'ore_terms'):
                for rec in groups[group]:
                    if rec['span'] == span and 'statement' not in rec:
                        rec['statement'] = sid
            window_levels.extend(levels_here)

    return {
        'schema': COMPOSITION_VERSION,
        'text_sha256': hashlib.sha256(text.encode('utf-8')).hexdigest(),
        'minerals': groups['minerals'],
        'host_rock': groups['host_rock'],
        'alteration': groups['alteration'],
        'ore_terms': groups['ore_terms'],
        'statements': statements,
        'commodities': list(commodities.values()),
        'by_level': by_level,
        'coverage': {'sentences': len(sents), 'sentences_with_minerals': len(statements)},
    }


# --------------------------------------------------------------- attachment
def attach(spec, composition=None, text=None):
    """Fold a composition into a parsed spec: ``spec['composition']`` is the
    :func:`compose` result and ``spec['minerals']`` the canonical names in
    first-seen order.  Pass the composition, or the text to compose it from
    (a bare string in the second position is taken as the text)."""
    if isinstance(composition, str):
        composition, text = None, composition
    spec = json.loads(json.dumps(spec))
    if composition is None:
        body = text if text is not None else spec.get('text') or ''
        composition = compose(body, spec)
    composition = json.loads(json.dumps(composition))
    spec['composition'] = composition
    spec['minerals'] = list(dict.fromkeys(m['name'] for m in composition.get('minerals') or []))
    cov = spec.setdefault('coverage', {})
    cov['minerals'] = len(composition.get('minerals') or [])
    cov['composition_statements'] = len(composition.get('statements') or [])
    return spec


# ------------------------------------------------------------------ objects
def _label(rec):
    return (rec.get('name') or '').strip() or rec.get('kind') or 'working'


def _on_working(rec, z):
    """The point on a placed working's segment at elevation ``z``: along an
    inclined or vertical working it is the crossing; on a level working (no
    vertical extent) it is the midpoint at the level's elevation."""
    s, e = rec['start'], rec['end']
    dz = e[2] - s[2]
    if abs(dz) > 1e-6:
        t = (z - s[2]) / dz
        t = 0.0 if t < 0.0 else 1.0 if t > 1.0 else t
        return (s[0] + t * (e[0] - s[0]), s[1] + t * (e[1] - s[1]), s[2] + t * dz)
    return ((s[0] + e[0]) / 2.0, (s[1] + e[1]) / 2.0, z)


def _station_at(label, station, placed, elements):
    """Where the level meets the workings.  With the builder's ``station``
    (agentbuild's own ``_station`` over its placement context) that is
    exact; without it the start of a working already placed on that level is
    the same point, because that is where the builder started it.  Neither
    is a guess; when neither knows, the answer is None."""
    if station is not None:
        return station(label)
    for p in placed:
        el = elements.get(p['element']) or {}
        if el.get('level') == label and el.get('kind') in LEVEL_KINDS:
            return tuple(p['start']), 'the start of the %s, placed on that level' % _label(p)
    return None, 'the elevation of the "%s" level is not fixed by any placed working' % label


def _collar_from(placed):
    for p in placed:
        if p.get('placement') == 'the collar':
            return tuple(p['start'])
    return None


def composition_points(spec, placed, name='Composition (from description)', station=None,
                       collar=None):
    """A PointSet (role ``samples``) with one point per level-tied statement.

    A statement whose sentence names a level sits at that level's elevation
    on the working the sentence names, or — when it names none — at the
    level's station; one that names a working but no level sits at the
    collar and says so; one with neither produces no point and stays in the
    manifest block.  ``placement`` on each point says which rule placed it.
    ``station(label) -> ((x, y, z), how) | (None, reason)`` and ``collar``
    come from the builder's context; without them the placed records are
    read for the same facts.
    """
    comp = spec.get('composition') or {}
    by_id = dict((p['element'], p) for p in placed)
    elements = dict((e['id'], e) for e in (spec.get('elements') or []))
    ps = PointSet(name=name, role='samples', color=[186, 104, 200])
    ps.metadata['schema'] = COMPOSITION_VERSION
    unplaced = []
    for st in comp.get('statements') or []:
        rec = by_id.get(st.get('element'))
        level = st.get('level')
        if level:
            xyz, how = _station_at(level, station, placed, elements)
            if xyz is None:
                unplaced.append({'statement': st['id'], 'level': level, 'reason': how})
                continue
            if rec is not None:
                x, y, z = _on_working(rec, xyz[2])
                placement = 'at the %s level on the %s' % (level, _label(rec))
            else:
                x, y, z = xyz
                placement = 'at the %s level: %s; the sentence names no working' % (level, how)
        elif rec is not None:
            c = collar if collar is not None else _collar_from(placed)
            if c is None:
                unplaced.append({'statement': st['id'], 'reason': 'no level stated and no collar placed'})
                continue
            x, y, z = c
            placement = 'at the collar: no level stated'
        else:
            unplaced.append({'statement': st['id'],
                             'reason': 'no level and no working named; kept in the manifest only'})
            continue
        ps.add(x, y, z, statement=st['id'], minerals=', '.join(st['minerals']), zone=st.get('zone'),
               level=level, element=st.get('element'), placement=placement, page=st.get('page'),
               confidence='described', quote=st['quote'])
    ps.metadata['unplaced'] = unplaced
    ps.metadata['note'] = ('minerals the description names, placed at the level its sentence '
                           'names; not samples, not assays, not a resource')
    ps.provenance = {'source': 'minerals named in the mine description',
                     'note': 'each point quotes its sentence; a statement with no level and no '
                             'working is listed in the manifest only'}
    return ps
