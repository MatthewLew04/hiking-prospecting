import json
import os
import unittest


ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), '..'))
CATALOG = os.path.join(ROOT, 'pipelines', 'config', 'national_sources.json')
DELIVERY_LABELS = frozenset({
    'pmtiles', 'remote_vector_tiles', 'remote_arcgis_query', 'aoi_ingest',
    'registered_source', 'link_only',
})


class NationalSourceContractTests(unittest.TestCase):
    @staticmethod
    def catalog():
        with open(CATALOG, encoding='utf-8') as source:
            return json.load(source)['sources']

    def test_delivery_labels_match_the_implemented_integrations(self):
        catalog = self.catalog()
        self.assertEqual(catalog['plss']['browser_delivery'],
                         'remote_arcgis_query')
        self.assertEqual(catalog['chronicling_america']['browser_delivery'],
                         'aoi_ingest')
        self.assertEqual(catalog['edgar']['browser_delivery'], 'link_only')
        self.assertEqual(catalog['sedar_plus']['browser_delivery'], 'link_only')
        self.assertIn('AOI', catalog['chronicling_america']['implementation_note'])
        self.assertIn('live', catalog['plss']['implementation_note'])

    def test_registered_land_sources_do_not_claim_browser_artifacts(self):
        catalog = self.catalog()
        expected = {'blm_sma', 'padus', 'eamlis'}
        self.assertEqual(
            {key for key in expected
             if catalog[key].get('browser_delivery') == 'registered_source'},
            expected)
        for key in sorted(expected):
            with self.subTest(source=key):
                note = catalog[key].get('implementation_note', '')
                self.assertIn('no ', note.lower())
                self.assertIn('browser layer', note)
                self.assertIn('PMTiles artifact', note)

    def test_catalog_delivery_labels_are_known(self):
        catalog = self.catalog()
        for key, entry in catalog.items():
            if 'browser_delivery' not in entry:
                continue
            with self.subTest(source=key):
                self.assertIn(entry['browser_delivery'], DELIVERY_LABELS)


if __name__ == '__main__':
    unittest.main()
