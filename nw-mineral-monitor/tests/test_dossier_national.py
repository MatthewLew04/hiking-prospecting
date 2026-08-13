import os
import sys
import unittest


ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.join(ROOT, 'pipelines'))

import dossier


class NationalDossierTests(unittest.TestCase):
    def test_research_queries_use_registry_state_and_prefill_edgar(self):
        links = dossier.links_for('Copper Queen Mine', None, {
            'state': 'AZ', 'recorder': None, 'sos_business_search': None,
        })
        by_label = {row['label']: row for row in links}
        chronicling = next(row for row in links
                           if row['label'].startswith('Chronicling America'))
        self.assertIn('Arizona', chronicling['url'])
        self.assertIn('#/q=Copper%20Queen%20Mine',
                      by_label['SEC EDGAR company filings']['url'])
        self.assertFalse(any('business-entity' in row['label'] for row in links))

    def test_business_search_is_state_generic_and_only_when_configured(self):
        links = dossier.links_for('Empire Mine', None, {
            'state': 'CA', 'recorder': None,
            'sos_business_search': 'https://bizfileonline.sos.ca.gov/search/business',
        })
        business = next(row for row in links if 'business-entity' in row['label'])
        self.assertTrue(business['label'].startswith('California '))
        self.assertNotIn('Idaho', business['label'])


if __name__ == '__main__':
    unittest.main()
