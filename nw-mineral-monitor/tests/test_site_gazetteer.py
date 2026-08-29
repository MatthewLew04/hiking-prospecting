"""Contract for the site-name gazetteer.

The ASK panel used to answer a question that named a mine with a count of the
state's whole archive, because nothing in the app could turn a site NAME into
a coordinate: query_sites reads the loaded map tiles, and resolve_place knows
only districts and towns. This index is what closed that gap, so the things
worth pinning are the ones that would quietly reopen it — an index that stops
being name-searchable, a source that starts contributing rows with no names,
or placeholder names that would win a fuzzy match against a real one.
"""
import importlib
import json
import os
import sys
import unittest

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.join(ROOT, 'pipelines'))
GAZ = os.path.join(ROOT, 'site', 'data', 'gazetteer')
INDEX = os.path.join(GAZ, 'index.json')

COLUMNS = ('nm', 'c', 'sx', 'd', 'src', 'id', 'x', 'y')


def _index():
    with open(INDEX, encoding='utf-8') as handle:
        return json.load(handle)


def _shard(state):
    with open(os.path.join(GAZ, f'{state}.json'), encoding='utf-8') as handle:
        return json.load(handle)


@unittest.skipUnless(os.path.exists(INDEX),
                     'gazetteer not built (run pipelines/build_site_gazetteer.py)')
class SiteGazetteerTests(unittest.TestCase):
    def test_index_and_shards_agree(self):
        index = _index()
        self.assertTrue(index['states'], 'gazetteer indexes no states')
        total = 0
        for state, entry in index['states'].items():
            shard = _shard(state)
            self.assertEqual(shard['state'], state)
            self.assertEqual(shard['n'], entry['n'],
                             f'{state} shard length disagrees with the index')
            for column in COLUMNS:
                self.assertEqual(len(shard[column]), shard['n'],
                                 f'{state}.{column} is not a full column')
            total += shard['n']
        self.assertEqual(index['n'], total)

    def test_every_row_is_name_searchable_and_placeable(self):
        """A row with no name cannot be resolved by name, and a row with no
        coordinate cannot hand the spatial tools anything — either one is a
        row that exists only to be counted, which is the failure mode this
        index was built to end."""
        for state in _index()['states']:
            shard = _shard(state)
            for i in range(shard['n']):
                name = shard['nm'][i]
                self.assertTrue(name and name.strip(),
                                f'{state} row {i} carries no name')
                # "Unnamed prospect" is a placeholder, not a name; indexing it
                # would let it win a fuzzy match against a real site.
                self.assertFalse(name.lower().startswith('unnamed'),
                                 f'{state} row {i} indexes a placeholder name')
                self.assertIsInstance(shard['x'][i], float)
                self.assertIsInstance(shard['y'][i], float)

    def test_source_labels_resolve(self):
        for state in _index()['states']:
            shard = _shard(state)
            self.assertTrue(shard['srcs'])
            for value in set(shard['src']):
                self.assertIn(value, range(len(shard['srcs'])),
                              f'{state} references an undeclared source index')

    def test_usmin_is_deliberately_absent(self):
        """USMIN rows carry a topo feature TYPE and a quadrangle, never a site
        name. Indexing them would make "adit" match 121,193 Nevada rows by
        their type word, so its absence is a decision and not an omission."""
        gazetteer = importlib.import_module('build_site_gazetteer')
        self.assertNotIn('usmin', gazetteer.GAZETTEER_SOURCES)
        self.assertEqual(gazetteer.GAZETTEER_SOURCES, ('mrds', 'stategeo'))

    def test_provenance_travels_with_every_shard(self):
        """An archive centroid that cannot say which dump it came from is not
        evidence, and the answer path quotes these back to the reader."""
        for state in _index()['states']:
            shard = _shard(state)
            self.assertTrue(shard['provenance'], f'{state} shard has no provenance')
            for source, entry in shard['provenance'].items():
                self.assertIn(source, shard['srcs'])
                self.assertTrue(entry.get('retrieved'),
                                f'{state}/{source} does not say when it was retrieved')
                self.assertLessEqual(entry['named'], entry['records'])

    def test_the_reported_failure_now_resolves(self):
        """"center star mine in idaho" is the question that shipped the wrong
        answer. The producing Tenmile record and the Yreka prospect that
        shares its name must both be present and must not be at the same
        place, because collapsing them would be its own wrong answer."""
        shard = _shard('ID')
        hits = [i for i, name in enumerate(shard['nm'])
                if 'center star' in name.lower()]
        self.assertGreaterEqual(len(hits), 2)
        tenmile = [i for i in hits if abs(shard['y'][i] - 45.808) < 0.01]
        self.assertTrue(tenmile, 'the Tenmile Center Star Mine is not indexed')
        self.assertTrue(any('Tenmile' in (shard['d'][i] or '') for i in tenmile))
        self.assertTrue([i for i in hits if abs(shard['y'][i] - 47.500) < 0.01],
                        'the Yreka-district Center Star is not indexed')


if __name__ == '__main__':
    unittest.main()
