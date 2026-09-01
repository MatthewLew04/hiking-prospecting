"""narrative.lexicon — the underground vocabulary census: words, not geometry."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'pipelines'))

from geomodel import narrative  # noqa: E402

TEXT = ('An adit driven N45E for 900 feet cuts the vein. On the 300 level a '
        'drift was extended 450 feet, and a winze sunk 120 feet below the '
        '400 level. The glory hole and two cross-cuts were opened from the '
        'portal.')


class LexiconTests(unittest.TestCase):

    def test_surface_forms_count_under_canonical_kinds(self):
        lx = narrative.lexicon(TEXT)
        self.assertEqual(lx['schema'], 'nwmm-lexicon/1')
        # "glory hole" is a pit by vocabulary, and the verbatim surface is kept
        self.assertEqual(lx['kinds']['pit']['surfaces'], {'glory hole': 1})
        # "cross-cuts" normalises to the crosscut kind, surface form as written
        self.assertEqual(lx['kinds']['crosscut']['surfaces'], {'cross-cuts': 1})
        self.assertEqual(lx['kinds']['adit']['count'], 1)
        self.assertEqual(lx['kinds']['winze']['count'], 1)

    def test_referent_uses_count_too(self):
        # "from the portal" is a referent, not an element — but it is a word,
        # and the lexicon is a census of the words
        lx = narrative.lexicon(TEXT)
        self.assertEqual(lx['kinds']['portal']['count'], 1)

    def test_levels_first_seen_order_with_spans(self):
        lx = narrative.lexicon(TEXT)
        labels = [lv['label'] for lv in lx['levels']]
        self.assertEqual(labels, ['300', '400'])
        for lv in lx['levels']:
            a, b = lv['span']
            self.assertIn(lv['label'], TEXT[a:b])

    def test_verbs_and_sentence_counts(self):
        lx = narrative.lexicon(TEXT)
        self.assertEqual(lx['verbs'].get('driven'), 1)
        self.assertEqual(lx['verbs'].get('sunk'), 1)
        self.assertEqual(lx['sentences'], 3)
        self.assertEqual(lx['mining_sentences'], 3)

    def test_nouns_stay_out_of_the_verb_census(self):
        # "adit", "level", "workings" mark mining sentences, but they are the
        # kinds census, not verbs
        lx = narrative.lexicon(TEXT + ' The workings are extensive.')
        for noun in ('adit', 'level', 'winze', 'portal', 'workings', 'shaft'):
            self.assertNotIn(noun, lx['verbs'])

    def test_a_numbered_level_is_one_label_not_two(self):
        # "No. 3 level" also matches the bare-number level pattern; the more
        # specific claim wins and the label is listed once
        lx = narrative.lexicon('Ore was mined from the No. 3 level of the shaft.')
        self.assertEqual([lv['label'] for lv in lx['levels']], ['No. 3'])

    def test_empty_text_is_an_empty_census_not_an_error(self):
        for text in ('', None):
            lx = narrative.lexicon(text)
            self.assertEqual(lx['kinds'], {})
            self.assertEqual(lx['verbs'], {})
            self.assertEqual(lx['levels'], [])
            self.assertEqual(lx['sentences'], 0)

    def test_deterministic(self):
        self.assertEqual(narrative.lexicon(TEXT), narrative.lexicon(TEXT))

    def test_lexicon_is_read_only_beside_parse(self):
        # the census must not disturb the parser's result for the same text
        before = narrative.parse(TEXT)
        narrative.lexicon(TEXT)
        self.assertEqual(narrative.parse(TEXT), before)


if __name__ == '__main__':
    unittest.main()
