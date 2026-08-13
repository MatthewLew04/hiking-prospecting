import json
import os
import sys
import unittest


ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.join(ROOT, 'pipelines'))

import build_base_metal_prices as builder  # noqa: E402
import gradeslib  # noqa: E402


class BaseMetalPriceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(builder.OUT, encoding='utf-8') as source:
            cls.data = json.load(source)

    def test_reviewed_series_are_contiguous_and_positive(self):
        self.assertEqual(self.data['status'], 'reviewed')
        for metal, last_year in (('Cu', 2020), ('Pb', 2021), ('Zn', 2022)):
            table = self.data['prices_usd_per_lb'][metal]
            self.assertEqual(sorted(map(int, table)), list(range(1900, last_year + 1)))
            self.assertTrue(all(isinstance(value, (int, float)) and value > 0
                                for value in table.values()))

    def test_provenance_is_checksum_pinned(self):
        books = self.data['source']['workbooks']
        for metal in ('Cu', 'Pb', 'Zn'):
            self.assertRegex(books[metal]['sha256'], r'^[0-9a-f]{64}$')
            self.assertTrue(books[metal]['landing_page'].startswith('https://www.usgs.gov/'))
            self.assertEqual(books[metal]['source_column'], 'Unit value ($/t)')

    def test_known_usgs_rows_and_nearest_year_lookup(self):
        self.assertAlmostEqual(self.data['prices_usd_per_lb']['Cu']['1900'],
                               357 / builder.LB_PER_METRIC_TON, places=8)
        self.assertAlmostEqual(self.data['prices_usd_per_lb']['Pb']['1900'],
                               100 / builder.LB_PER_METRIC_TON, places=8)
        self.assertAlmostEqual(self.data['prices_usd_per_lb']['Zn']['1900'],
                               97 / builder.LB_PER_METRIC_TON, places=8)
        self.assertEqual(gradeslib.base_metal_price('Cu', 1898),
                         self.data['prices_usd_per_lb']['Cu']['1900'])


if __name__ == '__main__':
    unittest.main()
