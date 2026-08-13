import json
import os
import sys
import tempfile
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlparse


ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.join(ROOT, 'pipelines'))

import webscrub


class NationalWebscrubTests(unittest.TestCase):
    def test_registry_supplies_full_state_search_name(self):
        self.assertEqual(webscrub.state_search_name('AZ'), 'Arizona')
        self.assertEqual(webscrub.state_search_name('ID'), 'Idaho')

    def test_settings_default_for_any_aoi_and_reject_bad_values(self):
        self.assertEqual(webscrub.webscrub_settings({}),
                         {'max_features': 60, 'chronam_rps': 0.8})
        with self.assertRaisesRegex(ValueError, 'max_features'):
            webscrub.webscrub_settings({'webscrub': {'max_features': True}})
        with self.assertRaisesRegex(ValueError, 'chronam_rps'):
            webscrub.webscrub_settings({'webscrub': {'chronam_rps': 0}})

    def test_name_gathering_survives_absent_legacy_state_snapshots(self):
        with tempfile.TemporaryDirectory() as temporary:
            os.makedirs(os.path.join(temporary, 'data', 'grades'))
            with open(os.path.join(temporary, 'data', 'grades', 'grades.json'),
                      'w', encoding='utf-8') as output:
                json.dump({'n': 0, 'st': [], 'x': [], 'y': [], 'name': []}, output)
            aoi = {'key': 'bisbee', 'state': 'AZ',
                   'bbox': [-111, 31, -109, 33],
                   'webscrub': {'seed_names': ['Copper Queen Mine']}}
            with mock.patch.object(webscrub, 'SITE', temporary), \
                    mock.patch.object(webscrub, 'load_build_input',
                                      side_effect=KeyError('not staged')):
                self.assertEqual(webscrub.gather_names(aoi),
                                 [('copper queen', 'Copper Queen Mine')])

    def test_google_books_query_uses_requested_registry_state(self):
        urls = []

        def response(url, **_kwargs):
            urls.append(url)
            return json.dumps({'items': []})

        webscrub._gb_fails[0] = 0
        with mock.patch.object(webscrub, 'cached_get', side_effect=response):
            self.assertEqual(webscrub.gbooks('Copper Queen', 'Arizona', 0.8), [])
        query = parse_qs(urlparse(urls[0]).query)['q'][0]
        self.assertIn('Arizona mining', query)
        self.assertNotIn('idaho mining', query.lower())


if __name__ == '__main__':
    unittest.main()
