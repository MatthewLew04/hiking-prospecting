import copy
import hashlib
import json
import os
import sys
import unittest


ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.join(ROOT, 'pipelines'))

import validate_national


def inventory(ids, instances):
    return {
        'status': 'complete_at_retrieval',
        'source_records': len(ids),
        'maxzoom_feature_instances': instances,
        'maxzoom_unique_tiled_ids': len(ids),
        'ids_sha256': hashlib.sha256(json.dumps(
            ids, separators=(',', ':')).encode()).hexdigest(),
    }


class PointBaselineInventoryTests(unittest.TestCase):
    def test_exact_unique_ids_and_seam_instances_pass(self):
        ids = [2, 7, 11]
        qa = validate_national.QA()
        validate_national._validate_national_point_id_inventory(
            qa, 'mrds', {'n': 3, 'source_id_inventory': inventory(ids, 4)},
            {'maxzoom_feature_ids': {'mrds': ids},
             'maxzoom_feature_instances': {'mrds': 4}}, 'mrds')
        self.assertEqual(qa.errors, [])

    def test_missing_id_rehashed_forgery_still_fails_count_reconciliation(self):
        ids = [2, 11]
        forged = inventory(ids, 3)
        forged['source_records'] = 3
        forged['maxzoom_unique_tiled_ids'] = 2
        qa = validate_national.QA()
        validate_national._validate_national_point_id_inventory(
            qa, 'usmin', {'n': 3, 'source_id_inventory': forged},
            {'maxzoom_feature_ids': {'usmin': ids},
             'maxzoom_feature_instances': {'usmin': 3}}, 'usmin')
        self.assertTrue(any('do not reconcile' in error for error in qa.errors))

    def test_hash_instance_schema_and_duplicate_order_are_enforced(self):
        ids = [2, 7, 11]
        base = inventory(ids, 4)
        mutations = (
            lambda row: row.update(ids_sha256='0' * 64),
            lambda row: row.update(maxzoom_feature_instances=3),
            lambda row: row.update(extra=True),
        )
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                row = copy.deepcopy(base)
                mutate(row)
                qa = validate_national.QA()
                validate_national._validate_national_point_id_inventory(
                    qa, 'mrds', {'n': 3, 'source_id_inventory': row},
                    {'maxzoom_feature_ids': {'mrds': ids},
                     'maxzoom_feature_instances': {'mrds': 4}}, 'mrds')
                self.assertTrue(qa.errors)
        qa = validate_national.QA()
        validate_national._validate_national_point_id_inventory(
            qa, 'mrds', {'n': 3, 'source_id_inventory': base},
            {'maxzoom_feature_ids': {'mrds': [2, 2, 11]},
             'maxzoom_feature_instances': {'mrds': 4}}, 'mrds')
        self.assertTrue(any('invalid or duplicated' in error for error in qa.errors))


if __name__ == '__main__':
    unittest.main()
