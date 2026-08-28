"""Failure-path tests for the national MLRS Lambda updater.

These tests deliberately use a small, behaviorally accurate S3 fake instead
of moto.  The updater deployment has no third-party runtime dependencies, and
the fake makes the exact If-None-Match/If-Match operations and write ordering
visible to assertions.
"""

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
    """Minimal botocore-style exception consumed by lambda_updater."""

    def __init__(self, code):
        self.response = {"Error": {"Code": str(code)}}
        super().__init__(str(code))


class FakeS3:
    """In-memory S3 with conditional-write and conditional-delete semantics."""

    def __init__(self):
        self.objects = {}
        self.calls = []
        self._version = 0
        self.denied_gets = set()
        self.failed_puts = {}
        self.race_on_delete = {}

    @staticmethod
    def _object_key(kwargs):
        return kwargs["Bucket"], kwargs["Key"]

    @staticmethod
    def _body_bytes(body):
        if isinstance(body, bytes):
            return body
        if isinstance(body, str):
            return body.encode()
        return body.read()

    def _store(self, object_key, body, **metadata):
        self._version += 1
        etag = f'"fake-etag-{self._version}"'
        self.objects[object_key] = {
            "Body": self._body_bytes(body),
            "ETag": etag,
            **metadata,
        }
        return etag

    def put_object(self, **kwargs):
        object_key = self._object_key(kwargs)
        self.calls.append(("put", kwargs["Key"], dict(kwargs)))
        if object_key in self.failed_puts:
            raise FakeClientError(self.failed_puts[object_key])
        current = self.objects.get(object_key)
        if kwargs.get("IfNoneMatch") == "*" and current is not None:
            raise FakeClientError("PreconditionFailed")
        if "IfMatch" in kwargs and (
                current is None or current["ETag"] != kwargs["IfMatch"]):
            raise FakeClientError("PreconditionFailed")
        etag = self._store(
            object_key,
            kwargs["Body"],
            ContentType=kwargs.get("ContentType"),
            CacheControl=kwargs.get("CacheControl"),
        )
        return {"ETag": etag}

    def get_object(self, **kwargs):
        object_key = self._object_key(kwargs)
        self.calls.append(("get", kwargs["Key"], dict(kwargs)))
        if object_key in self.denied_gets:
            raise FakeClientError("AccessDenied")
        if object_key not in self.objects:
            raise FakeClientError("NoSuchKey")
        current = self.objects[object_key]
        return {"Body": io.BytesIO(current["Body"]), "ETag": current["ETag"]}

    def delete_object(self, **kwargs):
        object_key = self._object_key(kwargs)
        self.calls.append(("delete", kwargs["Key"], dict(kwargs)))

        # Simulate a new owner refreshing/replacing the object after the
        # updater's GET but before its conditional DELETE reaches S3.
        if object_key in self.race_on_delete:
            replacement = self.race_on_delete.pop(object_key)
            self._store(object_key, json.dumps(replacement).encode())

        current = self.objects.get(object_key)
        if "IfMatch" in kwargs and (
                current is None or current["ETag"] != kwargs["IfMatch"]):
            raise FakeClientError("PreconditionFailed")
        self.objects.pop(object_key, None)
        return {}

    def put_json(self, bucket, key, value):
        self.put_object(Bucket=bucket, Key=key,
                        Body=json.dumps(value).encode(),
                        ContentType="application/json")

    def read_json(self, bucket, key):
        return json.loads(self.objects[(bucket, key)]["Body"])


class FakeLambda:
    def __init__(self):
        self.calls = []

    def invoke(self, **kwargs):
        self.calls.append(kwargs)
        return {"StatusCode": 202}


def _load_updater():
    """Import without constructing real AWS clients or consulting credentials."""
    fake_boto3 = types.ModuleType("boto3")
    bootstrap_s3 = FakeS3()
    bootstrap_lambda = FakeLambda()

    def client(service):
        return {"s3": bootstrap_s3, "lambda": bootstrap_lambda}[service]

    fake_boto3.client = client
    previous = sys.modules.get("boto3")
    sys.modules["boto3"] = fake_boto3
    try:
        spec = importlib.util.spec_from_file_location(
            "lambda_updater_under_test", os.path.join(INFRA, "lambda_updater.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        if previous is None:
            sys.modules.pop("boto3", None)
        else:
            sys.modules["boto3"] = previous


updater = _load_updater()


class LambdaUpdaterSafetyTests(unittest.TestCase):
    bucket = "unit-test-bucket"

    def setUp(self):
        self.s3 = FakeS3()
        self.lambda_client = FakeLambda()
        updater.s3 = self.s3
        updater.lam = self.lambda_client
        self.env = mock.patch.dict(
            os.environ,
            {"BUCKET": self.bucket, "AWS_LAMBDA_FUNCTION_NAME": "ClaimsUpdater"},
        )
        self.env.start()

    def tearDown(self):
        self.env.stop()

    def _valid_checkpoint(self, state="AZ", mode="active"):
        cols = {
            "serial": [], "name": [], "type": [], "x": [], "y": [],
            "admin_state": [], "geo_state": [], "disp": [], "acres": [],
        }
        value = {
            "_schema": updater.CHECKPOINT_SCHEMA,
            "clip_version": updater.CLIP_VERSION,
            "partition": 0,
            "cursor": None,
            "pages_total": 0,
            "total": None,
            "cols": cols,
        }
        self.s3.put_json(self.bucket, updater.ckpt_key(state, mode), value)
        return value

    @staticmethod
    def _empty_snapshot(state="AZ", mode="active"):
        return {
            "state": state, "layer": mode, "retrieved": "2026-08-13", "n": 0,
            "serial": [], "name": [], "type": [], "x": [], "y": [],
            "admin_state": [], "geo_state": [], "disp": [], "acres": [],
        }

    @staticmethod
    def _context():
        return types.SimpleNamespace(
            aws_request_id="aws-request-one",
            function_name="ClaimsUpdater",
            get_remaining_time_in_millis=lambda: 900_000,
        )

    def test_atomic_if_none_match_excludes_second_lock_owner(self):
        self.assertTrue(updater.lock_acquire(
            self.bucket, "NV", "active", "owner-one"))
        self.assertFalse(updater.lock_acquire(
            self.bucket, "NV", "active", "owner-two"))

        lock = self.s3.read_json(
            self.bucket, updater.lock_key("NV", "active"))
        self.assertEqual(lock["run_id"], "owner-one")
        lock_puts = [call for call in self.s3.calls
                     if call[0] == "put" and call[1] == updater.lock_key("NV", "active")]
        self.assertEqual(len(lock_puts), 2)
        self.assertTrue(all(call[2].get("IfNoneMatch") == "*" for call in lock_puts))

    def test_release_checks_owner_and_etag_across_delete_race(self):
        self.assertTrue(updater.lock_acquire(
            self.bucket, "NV", "closed", "owner-one"))
        lock_key = updater.lock_key("NV", "closed")

        # An unrelated invocation cannot release the current owner's lock.
        self.assertFalse(updater.lock_release(
            self.bucket, "NV", "closed", "owner-two"))
        self.assertIn((self.bucket, lock_key), self.s3.objects)

        # Even the former owner cannot delete a lock changed between GET and
        # DELETE: the stale ETag must lose the conditional request.
        self.s3.race_on_delete[(self.bucket, lock_key)] = {
            "run_id": "owner-two", "ts": 123.0,
        }
        self.assertFalse(updater.lock_release(
            self.bucket, "NV", "closed", "owner-one"))
        self.assertEqual(
            self.s3.read_json(self.bucket, lock_key)["run_id"], "owner-two")
        conditional_deletes = [call for call in self.s3.calls
                               if call[0] == "delete" and call[1] == lock_key]
        self.assertEqual(len(conditional_deletes), 1)
        self.assertIn("IfMatch", conditional_deletes[0][2])

    def test_incompatible_checkpoint_is_discarded(self):
        key = updater.ckpt_key("AZ", "active")
        self.s3.put_json(self.bucket, key, {
            "_schema": updater.CHECKPOINT_SCHEMA - 1,
            "clip_version": "pre-spatial-clip",
            "cursor": 99,
            "cols": {},
        })

        self.assertIsNone(updater.ckpt_load(
            self.bucket, "AZ", "active"))
        self.assertNotIn((self.bucket, key), self.s3.objects)

    def test_checkpoint_access_denied_is_not_treated_as_missing(self):
        key = updater.ckpt_key("AZ", "active")
        self.s3.denied_gets.add((self.bucket, key))

        with self.assertRaises(FakeClientError) as caught:
            updater.ckpt_load(self.bucket, "AZ", "active")
        self.assertEqual(caught.exception.response["Error"]["Code"], "AccessDenied")
        self.assertFalse(any(call[0] == "delete" and call[1] == key
                             for call in self.s3.calls))

    def test_malformed_current_checkpoint_fails_instead_of_restarting(self):
        value = self._valid_checkpoint()
        value["pages_total"] = -1
        self.s3.put_json(self.bucket, updater.ckpt_key("AZ", "active"), value)
        with self.assertRaisesRegex(RuntimeError, "malformed current checkpoint"):
            updater.ckpt_load(self.bucket, "AZ", "active")

    def test_snapshot_put_failure_retains_checkpoint_and_releases_lock(self):
        checkpoint_key = updater.ckpt_key("AZ", "active")
        lock_key = updater.lock_key("AZ", "active")
        snapshot_key = "staging/claims/az_active.json"
        self._valid_checkpoint()
        self.s3.failed_puts[(self.bucket, snapshot_key)] = "InternalError"
        self.s3.calls.clear()

        with mock.patch.object(
                updater, "pull_state",
                return_value=(self._empty_snapshot(), True)):
            with self.assertRaises(FakeClientError):
                updater.handler(
                    {"mode": "active", "states": ["AZ"], "run_id": "owner-one"},
                    self._context(),
                )

        self.assertIn((self.bucket, checkpoint_key), self.s3.objects)
        self.assertNotIn((self.bucket, lock_key), self.s3.objects)
        self.assertFalse(any(call[0] == "delete" and call[1] == checkpoint_key
                             for call in self.s3.calls))
        self.assertTrue(any(call[0] == "delete" and call[1] == lock_key
                            for call in self.s3.calls))

    def test_success_publishes_snapshot_before_deleting_checkpoint(self):
        checkpoint_key = updater.ckpt_key("AZ", "active")
        lock_key = updater.lock_key("AZ", "active")
        snapshot_key = "staging/claims/az_active.json"
        self._valid_checkpoint()
        self.s3.calls.clear()

        with mock.patch.object(
                updater, "pull_state",
                return_value=(self._empty_snapshot(), True)):
            result = updater.handler(
                {"mode": "active", "states": ["AZ"], "run_id": "owner-one"},
                self._context(),
            )

        operations = [(operation, key) for operation, key, _ in self.s3.calls]
        publish_index = operations.index(("put", snapshot_key))
        checkpoint_delete_index = operations.index(("delete", checkpoint_key))
        lock_delete_index = operations.index(("delete", lock_key))
        self.assertLess(publish_index, checkpoint_delete_index)
        self.assertLess(checkpoint_delete_index, lock_delete_index)
        self.assertIn((self.bucket, snapshot_key), self.s3.objects)
        self.assertNotIn((self.bucket, checkpoint_key), self.s3.objects)
        self.assertNotIn((self.bucket, lock_key), self.s3.objects)
        self.assertEqual(result["mode"], "active")

    def test_fetch_stops_for_time_budget_without_network(self):
        remaining = lambda: updater.TIME_RESERVE_MS + 5_000
        with mock.patch.object(
                updater.urllib.request, "urlopen",
                side_effect=AssertionError("network must not be opened")) as urlopen:
            with self.assertRaisesRegex(RuntimeError, "Lambda time budget"):
                updater.fetch("https://example.invalid/blm", ms_left=remaining)
        urlopen.assert_not_called()

    def test_production_default_never_publishes_statewide_json(self):
        self.assertEqual(updater.LEGACY_JSON_STATES, frozenset())
        with mock.patch.object(
                updater, "pull_state",
                return_value=(self._empty_snapshot(), True)):
            updater.handler(
                {"mode": "active", "states": ["AZ"], "run_id": "private-one"},
                self._context(),
            )
        put = next(call for call in self.s3.calls
                   if call[0] == "put" and
                   call[1] == "staging/claims/az_active.json")
        self.assertEqual(put[1], "staging/claims/az_active.json")
        self.assertEqual(put[2]["CacheControl"], "private, no-store")
        self.assertFalse(any(call[1].startswith("data/claims/")
                             for call in self.s3.calls))

    def test_production_closed_pull_is_uncapped(self):
        self.assertEqual(updater.CLOSED_CAP, 0)
        self.assertFalse(updater.closed_cap_reached("closed", 10_000_000))
        with open(os.path.join(INFRA, "template.yaml"), encoding="utf-8") as source:
            template = source.read()
        self.assertNotRegex(template, r"(?m)^\s+CLOSED_CAP:")

    def test_updater_role_can_list_the_bucket_so_a_missing_key_is_404(self):
        """The IAM grant lock_read() and ckpt_load() silently depend on.

        Both treat NoSuchKey/404/NotFound as "not there yet" and re-raise
        everything else -- see the AccessDenied tests above, which pin that
        on purpose. S3 only answers a GetObject for a nonexistent key with
        404 when the caller holds s3:ListBucket on the bucket; without it the
        answer is AccessDenied, and the first lock read of every run raises
        before a lock can be acquired. That was the 2026-08-21 outage.
        """
        with open(os.path.join(INFRA, "template.yaml"), encoding="utf-8") as source:
            template = source.read()
        role = template.split("UpdaterRole:", 1)[1].split("\n  UpdaterFunction:", 1)[0]
        self.assertRegex(
            role,
            r"(?m)^\s+-\s+Effect:\s+Allow\n\s+Action:\s+s3:ListBucket\n"
            r"\s+Resource:\s+!GetAtt\s+SiteBucket\.Arn\s*$",
            "UpdaterRole must grant s3:ListBucket on the bucket itself, or a "
            "missing lock/checkpoint returns AccessDenied instead of NoSuchKey")
        # A GetObject carries no prefix, so a condition on s3:prefix cannot be
        # satisfied by the implicit existence check and would leave the 403 in
        # place while looking like a tighter grant.
        list_stanza = role.split("Action: s3:ListBucket", 1)[1]
        self.assertNotIn("s3:prefix", list_stanza)

    def test_schedules_cover_every_claim_state_once_per_mode(self):
        with open(os.path.join(INFRA, "template.yaml"), encoding="utf-8") as source:
            template = source.read()
        events = []
        for encoded in re.findall(r"Input: '(\{[^'\n]+\})'", template):
            event = json.loads(encoded)
            if event.get("mode") in {"active", "closed"}:
                events.append(event)
        expected = set(updater.CLAIM_STATES)
        for mode in ("active", "closed"):
            rows = [state for event in events if event["mode"] == mode
                    for state in event["states"]]
            self.assertEqual(set(rows), expected)
            self.assertEqual(len(rows), len(expected),
                             f"{mode} schedules duplicate a claim state")

    def test_exhausted_pull_emits_clip_and_cursor_completion_evidence(self):
        row = dict(updater.CLAIM_STATES["AZ"])
        row["query_envelopes"] = [[-115.0, 31.0, -109.0, 38.0]]
        with mock.patch.dict(updater.CLAIM_STATES, {"AZ": row}), \
                mock.patch.object(updater, "ckpt_load", return_value=None), \
                mock.patch.object(updater, "fetch", return_value={"features": []}):
            snapshot, done = updater.pull_state(
                "AZ", "closed", self.bucket, lambda: 900_000)
        self.assertTrue(done)
        self.assertTrue(snapshot["pagination"]["complete"])
        self.assertEqual(snapshot["pagination"]["completed_envelopes"], 1)
        self.assertEqual(snapshot["pagination"]["terminal_empty_pages"], 1)
        self.assertEqual(snapshot["pagination"]["pages"], 0)
        self.assertEqual(snapshot["spatial_clip"]["artifact_sha256"],
                         updater.CLIP_SHA256)
        self.assertFalse(snapshot["truncated"])


if __name__ == "__main__":
    unittest.main()
