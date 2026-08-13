"""Safety and scope tests for the registry-driven national claim watch."""

import importlib.util
import io
import json
import os
import re
import sys
import types
import unittest
from unittest import mock


ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
INFRA = os.path.join(ROOT, "infra")
sys.path.insert(0, INFRA)


class FakeClientError(Exception):
    def __init__(self, code):
        self.response = {"Error": {"Code": str(code)}}
        super().__init__(str(code))


class FakeS3:
    def __init__(self):
        self.objects = {}
        self.calls = []
        self.version = 0
        self.denied_gets = set()

    @staticmethod
    def _key(kwargs):
        return kwargs["Bucket"], kwargs["Key"]

    @staticmethod
    def _bytes(value):
        if isinstance(value, bytes):
            return value
        if isinstance(value, str):
            return value.encode()
        return value.read()

    def put_object(self, **kwargs):
        key = self._key(kwargs)
        self.calls.append(("put", kwargs["Key"], dict(kwargs)))
        current = self.objects.get(key)
        if kwargs.get("IfNoneMatch") == "*" and current is not None:
            raise FakeClientError("PreconditionFailed")
        if "IfMatch" in kwargs and (
                current is None or current["ETag"] != kwargs["IfMatch"]):
            raise FakeClientError("PreconditionFailed")
        self.version += 1
        etag = f'"etag-{self.version}"'
        self.objects[key] = {
            "Body": self._bytes(kwargs["Body"]),
            "ETag": etag,
            "ContentType": kwargs.get("ContentType"),
            "CacheControl": kwargs.get("CacheControl"),
        }
        return {"ETag": etag}

    def get_object(self, **kwargs):
        key = self._key(kwargs)
        self.calls.append(("get", kwargs["Key"], dict(kwargs)))
        if key in self.denied_gets:
            raise FakeClientError("AccessDenied")
        if key not in self.objects:
            raise FakeClientError("NoSuchKey")
        value = self.objects[key]
        return {"Body": io.BytesIO(value["Body"]), "ETag": value["ETag"]}

    def delete_object(self, **kwargs):
        key = self._key(kwargs)
        self.calls.append(("delete", kwargs["Key"], dict(kwargs)))
        current = self.objects.get(key)
        if "IfMatch" in kwargs and (
                current is None or current["ETag"] != kwargs["IfMatch"]):
            raise FakeClientError("PreconditionFailed")
        self.objects.pop(key, None)
        return {}

    def read_json(self, bucket, key):
        return json.loads(self.objects[(bucket, key)]["Body"])

    def put_json(self, bucket, key, value):
        self.put_object(
            Bucket=bucket, Key=key, Body=json.dumps(value).encode(),
            ContentType="application/json")


class FakeLambda:
    def __init__(self):
        self.calls = []

    def invoke(self, **kwargs):
        self.calls.append(kwargs)
        return {"StatusCode": 202}


def _load_watch():
    fake_boto3 = types.ModuleType("boto3")
    bootstrap_s3 = FakeS3()
    bootstrap_lambda = FakeLambda()

    def client(service):
        if service == "s3":
            return bootstrap_s3
        if service == "lambda":
            return bootstrap_lambda
        return types.SimpleNamespace()

    fake_boto3.client = client
    previous = sys.modules.get("boto3")
    sys.modules["boto3"] = fake_boto3
    try:
        spec = importlib.util.spec_from_file_location(
            "national_watch_under_test", os.path.join(INFRA, "watch_lambda.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        if previous is None:
            sys.modules.pop("boto3", None)
        else:
            sys.modules["boto3"] = previous


watch = _load_watch()


class NationalWatchTests(unittest.TestCase):
    bucket = "watch-unit-test"

    def setUp(self):
        self.s3 = FakeS3()
        self.lambda_client = FakeLambda()
        watch.s3 = self.s3
        watch.lam = self.lambda_client
        self.environment = mock.patch.dict(os.environ, {
            "BUCKET": self.bucket,
            "AWS_LAMBDA_FUNCTION_NAME": "ExpirationWatch",
            "SES_FROM": "",
            "SES_TO": "",
            "WEBHOOK_URL": "",
            "SITE_URL": "https://map.example.test",
        })
        self.environment.start()

    def tearDown(self):
        self.environment.stop()

    @staticmethod
    def context():
        return types.SimpleNamespace(
            aws_request_id="watch-request-one",
            function_name="ExpirationWatch",
            get_remaining_time_in_millis=lambda: 900_000,
        )

    def test_runtime_claim_scope_is_exact_foundational_nineteen(self):
        expected = set("AK AZ AR CA CO FL ID LA MS MT NE NV NM ND OR SD UT WA WY".split())
        self.assertEqual(set(watch.CLAIM_STATES), expected)
        self.assertEqual(set(watch.WATCH_ORDER), expected)
        self.assertEqual(len(watch.WATCH_ORDER), 19)

    def test_cloudformation_federal_schedules_each_name_exact_nineteen(self):
        expected = set(watch.CLAIM_STATES)
        with open(os.path.join(INFRA, "template.yaml"), encoding="utf-8") as source:
            template = source.read()
        for resource in (
                "WatchDailyRule", "WatchSeasonalAugRule", "WatchSeasonalSepRule"):
            match = re.search(
                rf"^  {resource}:\n(?P<body>.*?)(?=^  [A-Za-z][A-Za-z0-9]+:\n)",
                template, re.M | re.S)
            self.assertIsNotNone(match, resource)
            input_match = re.search(r"Input: '([^']+)'", match.group("body"))
            self.assertIsNotNone(input_match, resource)
            payload = json.loads(input_match.group(1))
            self.assertEqual(set(payload["states"]), expected)
            self.assertEqual(len(payload["states"]), 19)

    def test_first_complete_snapshot_seeds_without_mass_new_filing_alerts(self):
        current = {
            "NV123": {"name": "Seed", "disp": "FILED", "trs": [], "xy": [-116, 39]},
        }
        digest = watch.build_state_digest(
            "NV", "daily", "run-1", current, None, None)
        self.assertTrue(digest["seeded"])
        self.assertIsNone(digest["previous_active"])
        self.assertEqual(digest["active_now"], 1)
        self.assertEqual(digest["alerts"], [])

    def test_complete_zero_is_distinct_from_unknown_and_can_close_prior_case(self):
        prior = watch._federal_snapshot("NV", {
            "NV123": {"name": "Prior", "disp": "ACTIVE", "trs": [], "xy": [-116, 39]},
        }, "2026-08-12T00:00:00+00:00")
        digest = watch.build_state_digest(
            "NV", "daily", "run-2", {}, prior, None)
        self.assertEqual(digest["active_now"], 0)
        self.assertEqual(digest["status"], "complete")
        self.assertEqual([row["kind"] for row in digest["alerts"]], ["ACTIVE→CLOSED"])

        marker = {
            "schema_version": 2, "run_id": "run-2", "mode": "daily",
            "states": list(watch.WATCH_ORDER), "complete": True,
            "state_digest_prefix": "data/alerts/runs/run-2/",
        }
        for state in watch.WATCH_ORDER:
            row = watch.build_state_digest(state, "daily", "run-2", {}, None, None)
            watch.put_json(
                self.bucket,
                watch.run_state_digest_key("run-2", state, public=True), row)
        ak = {
            "schema_version": watch.DIGEST_SCHEMA,
            "state": "AK", "system_id": "alaska_state_claims",
            "status": "complete", "active_now": 0, "alerts": [],
            "generated": "2026-08-13T00:00:00+00:00",
        }
        merged = watch.build_merged_digest(self.bucket, marker=marker, ak_digest=ak)
        self.assertEqual(merged["active_now"], 0)
        self.assertEqual(
            merged["systems"]["federal_mlrs"]["states"]["NV"]["active_now"], 0)

        self.s3.objects.pop((
            self.bucket,
            watch.run_state_digest_key("run-2", "NV", public=True)))
        incomplete = watch.build_merged_digest(self.bucket, marker=marker, ak_digest=ak)
        self.assertIsNone(incomplete["active_now"])
        self.assertIsNone(
            incomplete["systems"]["federal_mlrs"]["states"]["NV"]["active_now"])
        self.assertEqual(
            incomplete["systems"]["federal_mlrs"]["states"]["NV"]["status"],
            "unknown")

    def test_malformed_or_denied_prior_snapshot_never_becomes_empty(self):
        with self.assertRaises(RuntimeError):
            watch.build_state_digest(
                "NV", "daily", "run", {}, {"records": {}}, None)
        denied = (self.bucket, watch.snapshot_key("NV"))
        self.s3.denied_gets.add(denied)
        with self.assertRaises(FakeClientError):
            watch.s3_json(self.bucket, watch.snapshot_key("NV"))

    def test_subset_uses_private_snapshot_and_alert_only_public_digest(self):
        records = {
            "NV1": {"name": "One", "disp": "FILED", "trs": [], "xy": [-116, 39]},
        }
        with mock.patch.object(watch, "pull_state_cases", return_value=(records, True)):
            result = watch.handler_federal(
                {"mode": "daily", "states": ["NV"]}, self.context())
        self.assertEqual(result["status"], "complete_subset")
        snapshot = self.s3.read_json(self.bucket, watch.snapshot_key("NV"))
        digest = self.s3.read_json(
            self.bucket,
            watch.run_state_digest_key(result["run_id"], "NV"))
        self.assertIn("records", snapshot)
        self.assertNotIn("records", digest)
        self.assertEqual(snapshot["active_count"], 1)
        self.assertEqual(digest["active_now"], 1)
        self.assertNotIn((self.bucket, watch.state_digest_key("NV")), self.s3.objects)
        self.assertNotIn((self.bucket, "data/alerts/latest.json"), self.s3.objects)
        snapshot_put = next(
            call for call in self.s3.calls
            if call[0] == "put" and call[1] == watch.snapshot_key("NV"))
        self.assertEqual(snapshot_put[2]["CacheControl"], "private, no-store")

    def test_interrupted_pull_chains_without_publishing_zero(self):
        with mock.patch.object(watch, "pull_state_cases", return_value=(None, False)):
            result = watch.handler_federal(
                {"mode": "daily", "states": ["NV"]}, self.context())
        self.assertEqual(result["status"], "continued")
        self.assertIsNone(result["active"])
        self.assertEqual(result["unknown_states"], ["NV"])
        self.assertEqual(len(self.lambda_client.calls), 1)
        payload = json.loads(self.lambda_client.calls[0]["Payload"])
        self.assertEqual(payload["remaining_states"], ["NV"])
        self.assertNotIn((self.bucket, watch.state_digest_key("NV")), self.s3.objects)

    def test_national_continuation_keeps_every_state_lock_owned(self):
        with mock.patch.object(watch, "pull_state_cases", return_value=(None, False)):
            result = watch.handler_federal({"mode": "daily"}, self.context())
        self.assertEqual(result["status"], "continued")
        self.assertEqual(set(result["unknown_states"]), set(watch.CLAIM_STATES))
        for state in watch.WATCH_ORDER:
            lock = watch.lock_read(self.bucket, state)
            self.assertEqual(lock["run_id"], "watch-request-one")
        payload = json.loads(self.lambda_client.calls[0]["Payload"])
        self.assertEqual(set(payload["requested_states"]), set(watch.CLAIM_STATES))
        self.assertEqual(set(payload["remaining_states"]), set(watch.CLAIM_STATES))

    def test_same_run_retry_reuses_committed_diff_without_erasing_alert(self):
        prior = watch._federal_snapshot("NV", {
            "NV1": {"name": "Closed lead", "disp": "ACTIVE", "trs": [],
                    "xy": [-116, 39]},
        }, "2026-08-12T00:00:00+00:00", run_id="older-run")
        self.s3.put_json(self.bucket, watch.snapshot_key("NV"), prior)
        with mock.patch.object(watch, "pull_state_cases", return_value=({}, True)):
            first = watch.handler_federal(
                {"mode": "daily", "states": ["NV"], "run_id": "retry-run"},
                self.context())
        saved = self.s3.read_json(
            self.bucket, watch.run_state_digest_key("retry-run", "NV"))
        self.assertEqual([row["kind"] for row in saved["alerts"]], ["ACTIVE→CLOSED"])

        with mock.patch.object(watch, "pull_state_cases") as pull:
            second = watch.handler_federal({
                "mode": "daily", "requested_states": ["NV"],
                "remaining_states": ["NV"], "run_id": "retry-run", "chain": 1,
            }, self.context())
        pull.assert_not_called()
        self.assertEqual(first["status"], "complete_subset")
        self.assertEqual(second["status"], "complete_subset")
        preserved = self.s3.read_json(
            self.bucket, watch.run_state_digest_key("retry-run", "NV"))
        self.assertEqual(preserved["alerts"], saved["alerts"])

    def test_state_lock_requires_atomic_create_and_owner_checked_release(self):
        self.assertTrue(watch.lock_acquire(self.bucket, "NV", "owner-one"))
        self.assertFalse(watch.lock_acquire(self.bucket, "NV", "owner-two"))
        self.assertFalse(watch.lock_release(self.bucket, "NV", "owner-two"))
        self.assertTrue(watch.lock_release(self.bucket, "NV", "owner-one"))
        lock_puts = [
            call for call in self.s3.calls
            if call[0] == "put" and call[1] == watch.lock_key("NV")
        ]
        self.assertTrue(all(call[2].get("IfNoneMatch") == "*" for call in lock_puts))

    def test_ak_state_watcher_reports_busy_as_unknown_not_zero(self):
        self.assertTrue(watch.lock_acquire(
            self.bucket, "AK", "first-owner", system="alaska_state_claims"))
        result = watch.handler_ak_state({"mode": "daily"}, self.context())
        self.assertEqual(result["status"], "busy")
        self.assertFalse(result["complete"])
        self.assertIsNone(result["active"])
        self.assertIsNone(result["alerts"])

    def test_complete_national_run_writes_exact_state_digests_and_marker(self):
        def complete_pull(_state, _bucket, _ms_left):
            # A national run owns every state baseline until all private
            # snapshots/evidence inputs have been captured.
            for code in watch.WATCH_ORDER:
                lock = watch.lock_read(self.bucket, code)
                self.assertEqual(lock["run_id"], "watch-request-one")
            return {}, True

        with mock.patch.object(watch, "pull_state_cases", side_effect=complete_pull):
            result = watch.handler_federal({"mode": "daily"}, self.context())
        self.assertTrue(result["complete"])
        self.assertEqual(set(result["states"]), set(watch.CLAIM_STATES))
        self.assertEqual(result["active"], 0)
        marker = self.s3.read_json(self.bucket, "watch/federal/latest_run.json")
        self.assertEqual(set(marker["states"]), set(watch.CLAIM_STATES))
        for state in watch.CLAIM_STATES:
            digest = self.s3.read_json(self.bucket, watch.state_digest_key(state))
            self.assertEqual(digest["state"], state)
            self.assertEqual(digest["active_now"], 0)
            self.assertNotIn("records", digest)
        merged = self.s3.read_json(self.bucket, "data/alerts/latest.json")
        self.assertEqual(merged["systems"]["federal_mlrs"]["status"], "complete")
        # AK DNR has not seeded in this fixture, so the dual-system aggregate
        # is visibly incomplete rather than silently omitting that system.
        self.assertEqual(merged["systems"]["alaska_state_claims"]["status"], "unknown")
        self.assertFalse(merged["complete"])
        for state in watch.WATCH_ORDER:
            self.assertIsNone(watch.lock_read(self.bucket, state))

    def test_release_evidence_is_content_addressed_and_binds_all_systems(self):
        run_id = "release-run-1"
        generated = "2026-08-13T12:00:00+00:00"
        marker = {
            "schema_version": 2, "run_id": run_id, "generated": generated,
            "mode": "daily", "states": list(watch.WATCH_ORDER),
            "state_digest_prefix": f"data/alerts/runs/{run_id}/",
            "complete": True,
        }
        federal_snapshots = {}
        for state in watch.WATCH_ORDER:
            snapshot = watch._federal_snapshot(
                state, {}, generated, run_id=run_id)
            federal_snapshots[state] = snapshot
            watch.put_json(self.bucket, watch.snapshot_key(state), snapshot,
                           cache="private, no-store")
        alaska_snapshot = {
            "schema_version": watch.SNAPSHOT_SCHEMA,
            "system_id": "alaska_state_claims", "state": "AK",
            "generated": generated, "complete": True, "active_count": 1,
            "records": {"ADL1": {"serial": "ADL1"}},
        }
        watch.put_json(
            self.bucket, "watch/alaska_state/active.json", alaska_snapshot,
            cache="private, no-store")

        paths = watch.publish_release_evidence(self.bucket, marker)
        self.assertEqual(set(paths), set(watch.CLAIM_STATES))
        for state, path in paths.items():
            self.assertRegex(
                path,
                rf"^data/evidence/watch/{run_id}/{state.lower()}-[0-9a-f]{{64}}\.json$")
            evidence = self.s3.read_json(self.bucket, path)
            self.assertEqual(set(evidence), {
                "schema_version", "state", "run_id", "generated",
                "complete", "systems",
            })
            self.assertEqual(evidence["schema_version"], 1)
            self.assertEqual(evidence["state"], state)
            self.assertEqual(evidence["run_id"], run_id)
            self.assertEqual(evidence["generated"], generated)
            self.assertTrue(evidence["complete"])
            expected_systems = ({"federal_mlrs", "alaska_state_claims"}
                                if state == "AK" else {"federal_mlrs"})
            self.assertEqual(set(evidence["systems"]), expected_systems)
            self.assertNotIn("records", json.dumps(evidence))
            self.assertEqual(
                evidence["systems"]["federal_mlrs"]["source_snapshot_sha256"],
                watch.object_sha256(federal_snapshots[state]))
            expected_hash = watch.object_sha256(evidence)
            self.assertTrue(path.endswith(f"-{expected_hash}.json"))
        ak_evidence = self.s3.read_json(self.bucket, paths["AK"])
        self.assertEqual(
            ak_evidence["systems"]["alaska_state_claims"], {
                "status": "complete", "active_now": 1,
                "source_snapshot_sha256": watch.object_sha256(alaska_snapshot),
            })

    def test_release_evidence_withholds_all_states_without_ak_dual_snapshot(self):
        run_id = "release-run-2"
        generated = "2026-08-13T12:00:00+00:00"
        marker = {
            "schema_version": 2, "run_id": run_id, "generated": generated,
            "mode": "daily", "states": list(watch.WATCH_ORDER),
            "state_digest_prefix": f"data/alerts/runs/{run_id}/",
            "complete": True,
        }
        for state in watch.WATCH_ORDER:
            snapshot = watch._federal_snapshot(
                state, {}, generated, run_id=run_id)
            watch.put_json(self.bucket, watch.snapshot_key(state), snapshot,
                           cache="private, no-store")
        with self.assertRaisesRegex(RuntimeError, "Alaska state-claim snapshot"):
            watch.publish_release_evidence(self.bucket, marker)
        public_evidence = [
            key for bucket, key in self.s3.objects
            if bucket == self.bucket and key.startswith("data/evidence/watch/")
        ]
        self.assertEqual(public_evidence, [])


if __name__ == "__main__":
    unittest.main()
