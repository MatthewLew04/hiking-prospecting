"""Contract tests for tools/build_ws13_bundle.py.

The bundle is the code every WS13 node runs, fetched from a fixed S3 key at
boot. Two properties have to hold or the archive is worse than no archive,
and both have already failed here once:

  * IT IS CLOSED UNDER ITS OWN IMPORTS. A bundle built without ws13_migrate.py
    died on the seeding node with a bare ModuleNotFoundError, which is the
    whole reason ws13_seed.bundle_files() exists. The builder parses each
    member and refuses to write an archive that would do that again --
    test_a_dropped_member_fails_the_build removes a member and asserts the
    build fails BY NAME, because a check nobody has watched fail is not a
    check.
  * IT IS BYTE-REPRODUCIBLE. The bucket cannot be written from here and can
    barely be read, so "is the object in S3 the bundle these sources build?"
    has to be answerable by rebuilding and comparing digests. A tar that
    recorded the build host's mtimes, uid or gzip timestamp would make every
    rebuild a different answer.

The tests run the real builder over the real repository -- there is no
fixture corpus, because the property under test is a property of THIS file
list against THESE sources.
"""
from __future__ import annotations

import ast
import gzip
import hashlib
import io
import json
import re
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import build_ws13_bundle as bundle                          # noqa: E402


class MemberListTests(unittest.TestCase):
    def test_every_declared_source_exists(self):
        missing = [relative for _name, relative, _why in bundle.MEMBERS
                   if not (ROOT / relative).is_file()]
        self.assertEqual(missing, [], "MEMBERS names files that are gone")

    def test_no_two_members_extract_to_the_same_path(self):
        # The archive untars FLAT into /opt/ws13, so a duplicated basename
        # overwrites silently, on the node, at boot.
        names = [name for name, _relative, _why in bundle.MEMBERS]
        self.assertEqual(sorted(names), sorted(set(names)))

    def test_no_member_name_carries_a_directory(self):
        for name, _relative, _why in bundle.MEMBERS:
            self.assertNotIn("/", name)
            self.assertFalse(name.startswith("."))

    def test_every_member_records_why_it_ships(self):
        # The reason is the only thing that makes "can this be dropped?"
        # answerable a year from now.
        for name, _relative, why in bundle.MEMBERS:
            self.assertTrue(why.strip(), f"{name} ships for no stated reason")


class ClosureTests(unittest.TestCase):
    def test_the_repository_bundle_is_closed_under_its_imports(self):
        members = bundle.read_members()
        self.assertEqual(bundle.closure_problems(members), [])

    def test_a_dropped_member_fails_the_build_by_name(self):
        """The defect that made ws13_seed die on the node, re-run."""
        without_migrate = tuple(
            entry for entry in bundle.MEMBERS
            if entry[0] != "ws13_migrate.py")
        with mock.patch.object(bundle, "MEMBERS", without_migrate):
            members = bundle.read_members()
            problems = bundle.closure_problems(members)
        self.assertTrue(problems, "dropping ws13_migrate.py raised nothing")
        self.assertTrue(
            any("ws13_migrate" in problem for problem in problems),
            f"the failure does not name the missing module: {problems}")

    def test_a_function_level_sibling_import_still_counts(self):
        # ws13_seed imports its siblings inside main() on purpose, so that
        # bundle_files() can print a usable error before the ImportError. A
        # closure check that only read module-scope imports would find
        # nothing to require.
        source = (ROOT / "pipelines" / "ws13_seed.py").read_bytes()
        found = bundle.sibling_imports(source, "ws13_seed.py")
        self.assertIn("ws13_migrate", found)
        self.assertIn("ws13_backfill_provenance", found)

    def test_a_name_only_mentioned_in_prose_is_not_an_import(self):
        # Every module in this repository discusses its siblings by name in
        # comments and log lines. A grep-based closure check would demand
        # them all.
        source = (b'"""ws13_worker.py is discussed here."""\n'
                  b'# and here: ws13_enqueue.py\n'
                  b'print("ws13_rescue.py")\n')
        self.assertEqual(bundle.sibling_imports(source, "x.py"), set())

    def test_the_seed_declared_list_is_in_the_archive(self):
        members = bundle.read_members()
        self.assertEqual(bundle.seed_problems(members), [])

    def test_the_seed_declared_list_is_read_not_guessed(self):
        declared = bundle.declared_bundle_files()
        self.assertIn("ws13_migrations.sql", declared)
        self.assertIn("ws13_migrate.py", declared)


class FleetEntryPointTests(unittest.TestCase):
    """Every script the launch template invokes has to be in the archive.

    infra/ws13_fleet.yaml runs `python3 ws13_<x>.py` from /opt/ws13. A new
    entry point added to the template and not to MEMBERS is a node that boots,
    fails at `python3: can't open file`, and -- in confidence mode -- holds a
    claimed shard slot while doing nothing.

    Asserted against the BUILDER, not re-implemented here. A copy of the
    check living only in this file would let the documented step 0 --
    `tools/build_ws13_bundle.py` -- print "verified" and write the archive
    anyway, which is what it did when this test was the only place the
    template was ever read.
    """

    INVOCATION = re.compile(r"python3 (ws13_[a-z_]+\.py)")

    def test_the_template_invokes_only_members(self):
        members = bundle.read_members()
        self.assertEqual(bundle.template_problems(members), [])

    def test_the_check_reads_what_the_nodes_actually_run(self):
        """The invocations moved out of the template with the shell.

        node_boot.sh runs ws13_seed.py and run_worker.sh runs ws13_worker.py /
        ws13_confidence_pass.py; the template invokes nothing any more. A check
        that still read only the template would pass over an empty set, which
        is why template_problems() treats "found nothing anywhere" as a
        failure rather than as a clean result.
        """
        sources = [(ROOT / "infra" / "ws13_fleet.yaml").read_text("utf-8")]
        sources += [(ROOT / relative).read_text("utf-8")
                    for name, relative, _why in bundle.MEMBERS
                    if name.endswith(".sh")]
        invoked = set()
        for text in sources:
            invoked |= set(self.INVOCATION.findall(text))
        self.assertTrue(invoked, "found no `python3 ws13_*.py` anywhere")
        self.assertIn("ws13_worker.py", invoked)
        self.assertIn("ws13_confidence_pass.py", invoked)

    def test_an_entry_point_missing_from_members_fails_the_build(self):
        # The teeth: drop a script the template runs and the BUILD must stop.
        invoked = sorted(bundle.template_invocations())
        self.assertTrue(invoked)
        dropped = invoked[0]
        without = tuple(entry for entry in bundle.MEMBERS
                        if entry[0] != dropped)
        with mock.patch.object(bundle, "MEMBERS", without):
            problems = bundle.template_problems(bundle.read_members())
            with self.assertRaises(SystemExit) as caught:
                bundle.build()
        self.assertTrue(any(dropped in problem for problem in problems),
                        f"the failure does not name {dropped}: {problems}")
        self.assertIn(dropped, str(caught.exception))

    def test_a_template_that_cannot_be_read_is_a_failure_not_a_skip(self):
        # A check that silently passes because its input moved is the defect
        # it exists to catch.
        with mock.patch.object(bundle, "FLEET_TEMPLATE",
                               "infra/no-such-template.yaml"):
            problems = bundle.template_problems(bundle.read_members())
        self.assertTrue(problems)
        self.assertIn("not found", problems[0])


class ArchiveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.archive, cls.members, cls.manifest = bundle.build()

    def members_of(self, blob):
        with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tar:
            return {info.name: tar.extractfile(info).read()
                    for info in tar.getmembers() if info.isfile()}

    def test_two_builds_of_the_same_sources_are_byte_identical(self):
        again, _members, _manifest = bundle.build()
        self.assertEqual(hashlib.sha256(self.archive).hexdigest(),
                         hashlib.sha256(again).hexdigest())

    def test_the_gzip_header_carries_no_build_timestamp(self):
        # gzip writes the compression time into bytes 4..8 of its own header.
        # Left alone, that alone would make every rebuild a different digest.
        self.assertEqual(self.archive[4:8], b"\x00\x00\x00\x00")

    def test_no_tar_entry_records_the_build_host(self):
        raw = gzip.decompress(self.archive)
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r") as tar:
            for info in tar.getmembers():
                self.assertEqual(info.mtime, bundle.EPOCH, info.name)
                self.assertEqual((info.uid, info.gid), (0, 0), info.name)
                self.assertEqual((info.uname, info.gname), ("", ""), info.name)
                expected = (bundle.EXEC_MODE
                            if info.name.endswith(bundle.EXECUTABLE_SUFFIX)
                            else bundle.FILE_MODE)
                self.assertEqual(info.mode, expected, info.name)

    def test_the_archive_extracts_flat(self):
        for name in self.members_of(self.archive):
            self.assertNotIn("/", name)

    def test_every_member_is_present_with_its_source_bytes(self):
        extracted = self.members_of(self.archive)
        for name, relative, _why in bundle.MEMBERS:
            self.assertIn(name, extracted)
            self.assertEqual(extracted[name], (ROOT / relative).read_bytes(),
                             f"{name} is not the bytes of {relative}")

    def test_the_manifest_travels_with_the_code(self):
        # The fixed S3 key says nothing about its contents, so this file is
        # how a node answers "which bundle am I running?".
        extracted = self.members_of(self.archive)
        self.assertIn(bundle.MANIFEST_NAME, extracted)
        document = json.loads(extracted[bundle.MANIFEST_NAME])
        self.assertEqual(document["bundle"], bundle.KEY)
        recorded = {entry["name"]: entry for entry in document["members"]}
        self.assertEqual(sorted(recorded),
                         sorted(name for name, _r, _w in bundle.MEMBERS))
        for name, entry in recorded.items():
            data = extracted[name]
            self.assertEqual(entry["sha256"], hashlib.sha256(data).hexdigest())
            self.assertEqual(entry["bytes"], len(data))

    def test_the_manifest_names_the_source_each_member_came_from(self):
        document = json.loads(self.members_of(self.archive)
                              [bundle.MANIFEST_NAME])
        sources = {entry["name"]: entry["source"]
                   for entry in document["members"]}
        # The one member that is not from pipelines/ is the one most likely to
        # be mis-copied, since it is also the retrieval Lambda's handler.
        self.assertEqual(sources["ws13_query_lambda.py"],
                         "infra/ws13_query_lambda.py")


class VerifyTests(unittest.TestCase):
    def setUp(self):
        # addCleanup rather than enterContext: this suite has to run on the
        # 3.10 the project's own CI uses, and enterContext arrived in 3.11.
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.tmp = Path(holder.name)

    def test_a_freshly_built_archive_verifies(self):
        archive, _members, _manifest = bundle.build()
        path = self.tmp / "bundle.tar.gz"
        path.write_bytes(archive)
        self.assertEqual(bundle.verify(path), 0)

    def test_a_tampered_member_fails_verification(self):
        archive, _members, _manifest = bundle.build()
        raw = gzip.decompress(archive)
        out = io.BytesIO()
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r") as src, \
                tarfile.open(fileobj=out, mode="w") as dst:
            for info in src.getmembers():
                data = src.extractfile(info).read()
                if info.name == "ws13_worker.py":
                    data = data + b"\n# edited on the build host\n"
                    info.size = len(data)
                dst.addfile(info, io.BytesIO(data))
        path = self.tmp / "tampered.tar.gz"
        path.write_bytes(gzip.compress(out.getvalue(), mtime=0))
        self.assertEqual(bundle.verify(path), 1)

    def test_a_missing_archive_is_reported_not_raised(self):
        self.assertEqual(bundle.verify(self.tmp / "absent.tar.gz"), 2)


class UploadTests(unittest.TestCase):
    def test_the_builder_has_no_s3_write_path(self):
        """--upload prints a command; it must not be able to run one.

        The permission classifier refuses S3 writes to this bucket. A builder
        that grew an upload call would be working around that control rather
        than reporting it, so the absence is asserted rather than trusted.

        Parsed, not grepped, for the same reason the builder parses its
        members: this file's own prose says the words "boto3" and "upload"
        repeatedly, and a substring check would either fail on the
        explanation or be weakened until it proved nothing.
        """
        tree = ast.parse(
            (ROOT / "tools" / "build_ws13_bundle.py").read_text("utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        for forbidden in ("boto3", "botocore", "subprocess", "shutil"):
            self.assertNotIn(forbidden, imported,
                             f"the builder imports {forbidden}")
        called = {node.func.attr for node in ast.walk(tree)
                  if isinstance(node, ast.Call)
                  and isinstance(node.func, ast.Attribute)}
        for forbidden in ("put_object", "upload_file", "upload_fileobj",
                          "system", "run", "check_call", "Popen"):
            self.assertNotIn(forbidden, called,
                             f"the builder calls .{forbidden}()")

    def test_the_printed_key_is_the_key_the_fleet_downloads(self):
        template = (ROOT / "infra" / "ws13_fleet.yaml").read_text("utf-8")
        self.assertIn(f"/{bundle.KEY}", template)


if __name__ == "__main__":
    unittest.main()
