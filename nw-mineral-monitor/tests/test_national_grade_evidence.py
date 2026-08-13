"""Adversarial tests for the exact-49 WS9 grade-evidence compiler."""

import copy
import hashlib
import json
import os
import sys
import tempfile
import unittest
from unittest import mock


ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.join(ROOT, 'pipelines'))

import build_national_grade_evidence as grades


def _sha(seed):
    return hashlib.sha256(seed.encode()).hexdigest()


def _source(source_id, seed=None):
    seed = seed or source_id
    return {
        'source_id': source_id,
        'title': f'Primary report for {source_id}',
        'authority': 'Public geological authority',
        'url': f'https://example.test/{source_id}.pdf',
        'primary': True,
        'document_sha256': _sha(seed + '-document'),
        'page_index_sha256': _sha(seed + '-pages'),
        'citation': f'{source_id} reviewed primary report, page-level edition.',
        'publication_year': 1900,
    }


def _evidence(evidence_id, source_id, commodity='Au', value=1,
              unit='troy_ounces_per_short_ton'):
    return {
        'evidence_id': evidence_id,
        'source_id': source_id,
        'page_cite': 'p. 1',
        'verbatim_quote': f'Verbatim grade statement uniquely identifying {evidence_id}.',
        'quote_verbatim': True,
        'page_text_sha256': _sha(evidence_id + '-page'),
        'measurements': [{
            'commodity': commodity, 'value': value, 'unit': unit,
        }],
        'basis': 'reported sample',
        'years': '1900',
    }


class Fixture:
    def __init__(self, root):
        self.root = root
        self.private = os.path.join(root, 'private')
        self.publish = os.path.join(root, 'publish')
        self.inventory_path = os.path.join(self.private, 'inventory.json')
        os.makedirs(self.private)
        self.registry = grades.load_states()
        self.price_config = self._price_config()
        self._write('prices.json', self.price_config)
        self.states = {}
        for code in sorted(self.registry):
            source_id = f'source-{code.lower()}'
            grade_document = {
                'schema_version': 1,
                'state': code,
                'sources': [_source(source_id)],
                'mines': [{
                    'mine_id': f'{code.lower()}-mine-1',
                    'name': f'{code} reviewed mine one',
                    'district': f'{code} district',
                    'evidence': [_evidence(f'{code.lower()}-evidence-1', source_id)],
                }],
            }
            pp610_document = {
                'schema_version': 1,
                'state': code,
                'complete': True,
                'source': _source('pp610', 'pp610'),
                'districts': [],
                'no_district_finding': {
                    'finding': (
                        f'The reviewed PP 610 state section for {code} contains no '
                        'district row in this fixture; this is explicit, not silence.'),
                    'pages_reviewed': ['p. 1-2'],
                    'review_complete': True,
                },
            }
            grade_path = f'grades/{code.lower()}.json'
            pp610_path = f'pp610/{code.lower()}.json'
            self._write(grade_path, grade_document)
            self._write(pp610_path, pp610_document)
            self.states[code] = {
                'grades': self._descriptor(grade_path),
                'pp610': self._descriptor(pp610_path),
            }
        self.inventory = {
            'schema_version': 1,
            'dataset': grades.DATASET,
            'snapshot': '2026-08-13',
            'price_config': self._descriptor('prices.json'),
            'states': self.states,
        }
        self.write_inventory()

    @staticmethod
    def _price_config():
        values = {'Au': 20.0, 'Ag': 1.0, 'Cu': 0.1,
                  'Pb': 0.05, 'Zn': 0.05, 'Fe': 0.02}
        return {
            'schema_version': 1,
            'status': 'reviewed',
            'reviewed_on': '2026-08-13',
            'reviewed_by': 'Unit-test review board',
            'commodities': {
                commodity: {
                    'unit': grades.PRICE_UNITS[commodity],
                    'annual': {'1900': value},
                    'source': _source(f'price-{commodity.lower()}'),
                }
                for commodity, value in values.items()
            },
        }

    def _write(self, relative, value):
        path = os.path.join(self.private, relative)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as target:
            json.dump(value, target, sort_keys=True, separators=(',', ':'),
                      allow_nan=False)
        return path

    def _descriptor(self, relative):
        path = os.path.join(self.private, relative)
        return {
            'path': relative,
            'bytes': os.path.getsize(path),
            'sha256': grades.sha256_file(path),
        }

    def read(self, relative):
        with open(os.path.join(self.private, relative), encoding='utf-8') as source:
            return json.load(source)

    def rewrite_state(self, code, kind, value):
        relative = self.states[code][kind]['path']
        self._write(relative, value)
        self.states[code][kind] = self._descriptor(relative)
        self.inventory['states'] = self.states
        self.write_inventory()

    def add_low_endowment(self, code, valid=True):
        sources = []
        for index in range(2 if valid else 1):
            source = _source(f'{code.lower()}-finding-{index + 1}')
            source.update({
                'page_cite': f'p. {index + 10}',
                'verbatim_quote': (
                    f'Independent primary-source endowment statement {index + 1} '
                    f'for {code}.'),
                'quote_verbatim': True,
                'page_text_sha256': _sha(f'{code}-finding-page-{index}'),
            })
            sources.append(source)
        value = {
            'schema_version': 1,
            'state': code,
            'finding': (
                f'Two independent primary sources were reviewed for {code}; the '
                'documented endowment does not support a 25-mine grade set.'),
            'review_complete': True,
            'sources': sources,
        }
        relative = f'findings/{code.lower()}.json'
        self._write(relative, value)
        self.states[code]['low_endowment'] = self._descriptor(relative)
        self.inventory['states'] = self.states
        self.write_inventory()

    def write_inventory(self):
        os.makedirs(os.path.dirname(self.inventory_path), exist_ok=True)
        with open(self.inventory_path, 'w', encoding='utf-8') as target:
            json.dump(self.inventory, target, sort_keys=True, separators=(',', ':'),
                      allow_nan=False)


class NationalGradeEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.fixture = Fixture(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def _published_state(self, code):
        checked = grades.validate_pointer(self.fixture.publish)
        descriptor = checked['run']['state_evidence'][code]
        with open(os.path.join(self.fixture.publish, descriptor['file']),
                  encoding='utf-8') as source:
            return json.load(source)

    def test_progress_build_is_exact_49_content_addressed_and_honest(self):
        result = grades.build(self.fixture.inventory_path, self.fixture.publish)
        self.assertEqual(result['states'], 49)
        self.assertEqual(len(result['incomplete_states']), 49)
        self.assertEqual(result['done_gate_eligible_states'], 0)
        self.assertEqual(result['national_metrics']['graded_mines'], 49)
        checked = grades.validate_pointer(self.fixture.publish)
        self.assertEqual(set(checked['run']['state_evidence']), set(self.fixture.registry))
        nv_ref = checked['run']['state_evidence']['NV']
        self.assertEqual(
            nv_ref['bytes'],
            os.path.getsize(os.path.join(self.fixture.publish, nv_ref['file'])))
        self.assertFalse(checked['pointer']['all_states_done_gate_eligible'])
        state = self._published_state('NV')
        self.assertEqual(state['metrics']['graded_mines'], 1)
        self.assertEqual(state['metrics']['primary_sources'], 1)
        self.assertEqual(state['metrics']['verbatim_quotes'], 1)
        self.assertEqual(state['metrics']['page_cites'], 1)
        self.assertEqual(state['grade_requirement']['status'], 'incomplete')
        self.assertEqual(state['effect'], 'evidence_only_no_release_mutation')
        encoded = json.dumps(checked, sort_keys=True)
        self.assertNotIn('"enabled"', encoded)
        self.assertNotIn('.geojson', encoded.lower())
        for root, _dirs, files in os.walk(self.fixture.publish):
            self.assertTrue(all(name.endswith('.json') for name in files), root)

    def test_historic_value_per_ton_normalizes_all_six_reviewed_commodities(self):
        code = 'NV'
        source_id = 'source-nv'
        values = {'Au': 40.0, 'Ag': 3.0, 'Cu': 4.0,
                  'Pb': 2.0, 'Zn': 1.0, 'Fe': 0.8}
        mines = []
        for index, (commodity, dollars) in enumerate(values.items(), start=1):
            evidence = _evidence(f'nv-historic-{commodity.lower()}', source_id)
            evidence.pop('measurements')
            evidence['historic_values'] = [{
                'commodity': commodity,
                'value': dollars,
                'unit': 'nominal_usd_per_short_ton',
                'price_year': 1900,
            }]
            mines.append({
                'mine_id': f'nv-historic-mine-{index}',
                'name': f'Nevada historic {commodity} mine',
                'district': f'Nevada district {index}',
                'evidence': [evidence],
            })
        document = {'schema_version': 1, 'state': code,
                    'sources': [_source(source_id)], 'mines': mines}
        self.fixture.rewrite_state(code, 'grades', document)
        grades.build(self.fixture.inventory_path, self.fixture.publish)
        state = self._published_state(code)
        normalized = {
            row['normalized_measurements'][0]['commodity']:
            row['normalized_measurements'][0]
            for mine in state['mines'] for row in mine['evidence']
        }
        expected = {'Au': 2.0, 'Ag': 3.0, 'Cu': 2.0,
                    'Pb': 2.0, 'Zn': 1.0, 'Fe': 2.0}
        self.assertEqual(set(normalized), set(grades.COMMODITIES))
        for commodity, value in expected.items():
            self.assertAlmostEqual(normalized[commodity]['value'], value)
            self.assertEqual(normalized[commodity]['unit'],
                             grades.CANONICAL_GRADE_UNITS[commodity])
            self.assertEqual(normalized[commodity]['price']['year'], 1900)
            self.assertEqual(
                normalized[commodity]['price']['price_config_sha256'],
                self.fixture.inventory['price_config']['sha256'])

    def test_two_primary_source_finding_is_explicit_alternative_not_silence(self):
        self.fixture.add_low_endowment('AL')
        grades.build(self.fixture.inventory_path, self.fixture.publish)
        state = self._published_state('AL')
        self.assertEqual(state['grade_requirement']['status'],
                         'documented_low_endowment')
        self.assertTrue(state['grade_requirement']['done_gate_eligible'])
        self.assertEqual(len(state['low_endowment_finding']['sources']), 2)
        self.assertIn('AL', grades.validate_pointer(
            self.fixture.publish)['run']['done_gate_eligible_states'])

        bad = Fixture(os.path.join(self.temporary.name, 'second'))
        bad.add_low_endowment('AL', valid=False)
        with self.assertRaisesRegex(grades.PublicationError, 'at least two primary'):
            grades.build(bad.inventory_path, bad.publish)

    def test_quantitative_requirement_needs_25_mines_and_two_sources(self):
        metrics = {
            'graded_mines': 25, 'primary_sources': 2,
            'verbatim_quotes': 25, 'page_cites': 25,
        }
        requirement = grades._grade_requirement(metrics, None)
        self.assertEqual(requirement['status'], 'meets_quantitative_bar')
        self.assertTrue(requirement['done_gate_eligible'])
        one_source = dict(metrics, primary_sources=1)
        self.assertEqual(grades._grade_requirement(
            one_source, None)['status'], 'incomplete')

    def test_require_done_fails_before_replacing_progress_pointer(self):
        grades.build(self.fixture.inventory_path, self.fixture.publish)
        pointer_path = os.path.join(self.fixture.publish, 'latest.json')
        with open(pointer_path, 'rb') as source:
            before = source.read()
        with self.assertRaisesRegex(grades.PublicationError,
                                    'DONE-gate evidence is incomplete'):
            grades.build(self.fixture.inventory_path, self.fixture.publish,
                         require_done=True)
        with open(pointer_path, 'rb') as source:
            self.assertEqual(source.read(), before)

    def test_duplicate_bad_unit_quote_and_primary_source_fail_closed(self):
        document = self.fixture.read('grades/al.json')
        prices = grades.validate_price_config(self.fixture.price_config)
        price_sha = self.fixture.inventory['price_config']['sha256']

        duplicate = copy.deepcopy(document)
        duplicate['mines'].append(copy.deepcopy(duplicate['mines'][0]))
        with self.assertRaisesRegex(grades.PublicationError, 'duplicate mine_id'):
            grades.validate_grade_document(duplicate, 'AL', prices, price_sha)

        bad_unit = copy.deepcopy(document)
        bad_unit['mines'][0]['evidence'][0]['measurements'][0]['unit'] = 'ounces'
        with self.assertRaisesRegex(grades.PublicationError, 'measurement unit'):
            grades.validate_grade_document(bad_unit, 'AL', prices, price_sha)

        not_verbatim = copy.deepcopy(document)
        not_verbatim['mines'][0]['evidence'][0]['quote_verbatim'] = False
        with self.assertRaisesRegex(grades.PublicationError,
                                    'quote_verbatim must be true'):
            grades.validate_grade_document(not_verbatim, 'AL', prices, price_sha)

        secondary = copy.deepcopy(document)
        secondary['sources'][0]['primary'] = False
        with self.assertRaisesRegex(grades.PublicationError, 'primary source'):
            grades.validate_grade_document(secondary, 'AL', prices, price_sha)

    def test_pp610_requires_unique_cited_districts_or_explicit_zero_finding(self):
        document = self.fixture.read('pp610/al.json')
        document.pop('no_district_finding')
        with self.assertRaisesRegex(grades.PublicationError,
                                    'no_district_finding must be an object'):
            grades.validate_pp610_document(document, 'AL')
        district = {
            'district_id': 'al-district', 'name': 'Alabama district',
            'page_cite': 'p. 12',
            'verbatim_quote': 'A verbatim PP 610 district statement for Alabama.',
            'quote_verbatim': True,
            'page_text_sha256': _sha('pp610-al-page'),
        }
        document['districts'] = [district, copy.deepcopy(district)]
        with self.assertRaisesRegex(grades.PublicationError,
                                    'duplicate district_id'):
            grades.validate_pp610_document(document, 'AL')

    def test_inventory_rejects_unknown_state_bad_hash_and_public_staging(self):
        unknown = copy.deepcopy(self.fixture.inventory)
        unknown['states']['XX'] = unknown['states'].pop('AL')
        self.fixture.inventory = unknown
        self.fixture.write_inventory()
        with self.assertRaisesRegex(grades.PublicationError, 'unknown='):
            grades.load_inventory(self.fixture.inventory_path)

        self.fixture = Fixture(os.path.join(self.temporary.name, 'hash-fixture'))
        with open(os.path.join(self.fixture.private, 'grades/al.json'),
                  'a', encoding='utf-8') as target:
            target.write(' ')
        with self.assertRaisesRegex(grades.PublicationError, 'checksum/size mismatch'):
            grades.load_inventory(self.fixture.inventory_path)

        self.fixture = Fixture(os.path.join(self.temporary.name, 'public-fixture'))
        with mock.patch.object(grades, 'SITE', self.fixture.private):
            with self.assertRaisesRegex(grades.PublicationError, 'inside public site'):
                grades.load_inventory(self.fixture.inventory_path)

    def test_price_config_exact_year_units_and_fe_are_review_gated(self):
        missing_fe = copy.deepcopy(self.fixture.price_config)
        missing_fe['commodities'].pop('Fe')
        with self.assertRaisesRegex(grades.PublicationError, 'cover exactly'):
            grades.validate_price_config(missing_fe)
        bad_fe = copy.deepcopy(self.fixture.price_config)
        bad_fe['commodities']['Fe']['unit'] = 'usd_per_long_ton_ore'
        with self.assertRaisesRegex(grades.PublicationError, 'unit must be'):
            grades.validate_price_config(bad_fe)
        prices = grades.validate_price_config(self.fixture.price_config)
        historic = {'commodity': 'Fe', 'value': 1, 'unit':
                    'nominal_usd_per_short_ton', 'price_year': 1901}
        with self.assertRaisesRegex(grades.PublicationError,
                                    'absent from the reviewed annual series'):
            grades._normalize_historic(
                historic, prices, self.fixture.inventory['price_config']['sha256'])

    def test_input_mutation_during_build_does_not_create_latest_pointer(self):
        def mutate(_context):
            with open(os.path.join(self.fixture.private, 'grades/al.json'),
                      'a', encoding='utf-8') as target:
                target.write(' ')

        with self.assertRaisesRegex(grades.PublicationError, 'changed during build'):
            grades.build(self.fixture.inventory_path, self.fixture.publish,
                         before_commit=mutate)
        self.assertFalse(os.path.exists(
            os.path.join(self.fixture.publish, 'latest.json')))

    def test_strict_json_rejects_duplicate_keys_and_nan(self):
        with self.assertRaisesRegex(grades.PublicationError, 'duplicate JSON'):
            grades.strict_json_bytes(b'{"a":1,"a":2}', 'fixture')
        with self.assertRaisesRegex(grades.PublicationError,
                                    'non-standard JSON number'):
            grades.strict_json_bytes(b'{"a":NaN}', 'fixture')


if __name__ == '__main__':
    unittest.main()
