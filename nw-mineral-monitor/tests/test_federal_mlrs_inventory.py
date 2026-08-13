import json
import os
import sys
import tempfile
import unittest


ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.join(ROOT, 'pipelines'))

import build_federal_mlrs_inventory as compiler
from tests.test_federal_mlrs_pmtiles import Fixture, _write_json


class FederalMlrsInventoryTests(unittest.TestCase):
    def test_compiles_exact_38_complete_machine_attested_snapshots(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(directory)
            inventory = compiler.compile_inventory(
                fixture.staging, created='2026-08-13')
            with open(fixture.inventory_path, encoding='utf-8') as source:
                on_disk = json.load(source)
        self.assertEqual(inventory, on_disk)
        self.assertEqual(len(inventory['states']), 19)
        self.assertTrue(all(row[mode]['complete']
                            for row in inventory['states'].values()
                            for mode in ('active', 'closed')))

    def test_legacy_or_truncated_snapshot_is_explicitly_partial(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(directory)
            path = os.path.join(fixture.staging, 'ar_active.json')
            data = fixture.snapshots[('AR', 'active')]
            for field in ('source', 'spatial_clip', 'pagination'):
                data.pop(field)
            _write_json(path, data)
            closed = fixture.snapshots[('AZ', 'closed')]
            closed['truncated'] = True
            _write_json(os.path.join(fixture.staging, 'az_closed.json'), closed)
            inventory = compiler.compile_inventory(
                fixture.staging, created='2026-08-13')
        self.assertFalse(inventory['states']['AR']['active']['complete'])
        self.assertIn('attestation',
                      inventory['states']['AR']['active']['partial_reason'])
        self.assertFalse(inventory['states']['AZ']['closed']['complete'])
        self.assertIn('truncated',
                      inventory['states']['AZ']['closed']['partial_reason'])

    def test_invalid_snapshot_does_not_replace_existing_inventory(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(directory)
            original = b'preserve-this-private-inventory'
            with open(fixture.inventory_path, 'wb') as target:
                target.write(original)
            path = os.path.join(fixture.staging, 'co_active.json')
            data = fixture.snapshots[('CO', 'active')]
            data['state'] = 'NV'
            _write_json(path, data)
            with self.assertRaisesRegex(compiler.InventoryError, 'identity'):
                compiler.compile_inventory(fixture.staging, created='2026-08-13')
            with open(fixture.inventory_path, 'rb') as source:
                self.assertEqual(source.read(), original)


if __name__ == '__main__':
    unittest.main()
