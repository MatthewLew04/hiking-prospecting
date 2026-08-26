"""Contract tests for the WS13 fleet shell in infra/fleet/.

Five scripts run a node: node_boot.sh (what UserData execs), claim_slot.sh,
run_worker.sh, start_workers.sh and node_agent.sh. They had no tests at all,
which is how three blockers survived in the `FleetMode: confidence` slot-claim
protocol:

  1. A replacement for a hard-dead node arrived long before ClaimStaleSeconds,
     found no free slot, and retired WITH
     --should-decrement-desired-capacity -- permanently removing the only
     capacity that could ever reclaim the dead node's shard.
  2. Exit status 1 was both the pass's "this shard has work left" and
     CPython's status for any uncaught exception. The sweep loop read both as
     "sweep again", with no backoff, for the full 24 h ceiling.
  3. Nothing asserted every slot reached 'complete'. Once the group
     decremented to zero, "run finished" and "run finished with slots nobody
     ever claimed" were the same observable.

...and a fourth that no amount of testing the shell would have caught, because
it was about where the shell lived: as heredocs inside the LaunchTemplate's
UserData, the rendered block reached ~30,000 bytes against EC2's 16,384-byte
limit, so the stack could not be created and `FleetMode: confidence` had never
been deployable. The scripts now ship in ws13/fleet/bundle.tar.gz, UserData is
the ~40 lines that fetch it, and UserDataSizeTests holds that line.

These are protocol properties, so they are tested by RUNNING the scripts, not
by grepping them. The harness copies the committed files to a relocated
/opt/ws13, exports what UserData exports, and puts stubs for `aws`, `date`,
`sleep`, `curl` and `python3` at the front of PATH:

  * `aws` is an S3 object store in a directory plus a call log, so a claim is
    a real conditional put and two nodes racing for one slot really do race.
  * `date` stands in for GNU date, which is what Amazon Linux has. macOS date
    has no `-d`, and claim_slot.sh reads a claim's age with
    `date -d "$lastmodified" +%s`; without the stub every claim would parse as
    epoch 0 and look infinitely stale, which is exactly the branch under test.
  * `sleep` records its argument and returns at once, so a backoff schedule is
    asserted exactly instead of waited out. A test that exercises one of the
    two `poll every 60 s until a deadline` waits also sets
    `box.virtual_clock = True`, which makes that same stub advance a clock
    `date +%s` reads: without it the loop spins for the full 2,100 real
    seconds, and with it the loop takes the ~35 iterations it would really
    take and the wait itself becomes assertable. It is opt-in because the
    claim-age tests written before it depend on ages moving with real time.

The values !Sub used to bake in now arrive as environment. FLEET_ENV is what
UserData exports, derived from the same SUBS the rendered UserData uses, and
test_every_name_the_scripts_read_is_exported_by_user_data checks the scripts
against the TEMPLATE rather than against that list -- a name that stops being
exported is the empty string on the node, not a deploy-time error any more.

node_agent.sh's adopt step is exercised with start_workers.sh stubbed by a
recorder -- it is a separate file with a defined contract, and it gets its own
test -- so the agent can be run to completion without launching workers.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "infra" / "ws13_fleet.yaml"

# What !Sub resolves at deploy time. The numbers are the template's own
# defaults, so the rendered shell is the shell a default confidence deploy
# runs.
SUBS = {
    "BucketName": "nw-mineral-monitor-730883236375",
    "QueueUrl":
        "https://sqs.us-west-2.amazonaws.com/730883236375/ws13-ocr-work",
    "QueueName": "ws13-ocr-work",
    "DeadLetterQueueName": "ws13-ocr-dlq",
    "FleetName": "ws13-workers",
    "FleetMode": "confidence",
    "ConfidenceNodeSlots": "4",
    "ClaimStaleSeconds": "1800",
    "WorkersPerNode": "2",
    "ConfidenceSweepDocs": "25",
    "ConfidenceSweepMaxSeconds": "86400",
    "DbSecretArn": "arn:aws:secretsmanager:us-west-2:730883236375:secret:x",
    "DbEndpoint": "nwmm-ws13.example.us-west-2.rds.amazonaws.com",
    "AWS::Region": "us-west-2",
    "AWS::AccountId": "730883236375",
}
SLOTS = int(SUBS["ConfidenceNodeSlots"])
STALE = int(SUBS["ClaimStaleSeconds"])
# What SlotCompletionTests runs node_agent.sh with. See agent() for why it is
# not STALE: the adopt wait is polled in virtual 60 s steps, and 1800 + 300 of
# them is several hundred forked stubs per test.
AGENT_STALE = 120

FLEET_SHELL = ROOT / "infra" / "fleet"
# The five scripts the bundle untars into /opt/ws13. node_boot.sh is what
# UserData execs; the other four are what it and the agent then run.
SCRIPT_NAMES = ("node_boot.sh", "claim_slot.sh", "run_worker.sh",
                "start_workers.sh", "node_agent.sh")


# The exports UserData performs, derived from the same SUBS the rendered
# UserData uses. A name the scripts read and this does not carry is empty on
# the node, which test_every_name_the_scripts_read_is_exported checks against
# the template rather than against this list.
FLEET_ENV = {
    "WS13_BUCKET": SUBS["BucketName"],
    "WS13_QUEUE_URL": SUBS["QueueUrl"],
    "WS13_MODE": SUBS["FleetMode"],
    "WS13_FLEET_NAME": SUBS["FleetName"],
    "WS13_NODE_SLOTS": SUBS["ConfidenceNodeSlots"],
    "WS13_CLAIM_STALE": SUBS["ClaimStaleSeconds"],
    "WS13_WORKERS_PER_NODE": SUBS["WorkersPerNode"],
    "WS13_SWEEP_DOCS": SUBS["ConfidenceSweepDocs"],
    "WS13_SWEEP_MAX_SECONDS": SUBS["ConfidenceSweepMaxSeconds"],
    "WS13_DB_DSN": "postgresql://nwmm:pw@db:5432/nwmm?sslmode=require",
}


def fleet_scripts():
    """The committed scripts, read as files.

    They used to be carved back out of the template's UserData heredocs,
    because that is where they lived. Reading the real files instead is not
    just tidier: the heredoc extraction could only ever test a copy, and the
    copy is what stopped existing -- the UserData had grown to ~30,000 bytes
    against EC2's 16,384-byte limit, so the template these tests were passing
    over could not create a LaunchTemplate at all.
    """
    return {name: (FLEET_SHELL / name).read_text() for name in SCRIPT_NAMES}


def render_user_data():
    """The LaunchTemplate UserData with !Sub applied, as a node receives it."""
    text = TEMPLATE.read_text()
    marker = "        UserData:\n          Fn::Base64: !Sub |\n"
    body = text[text.index(marker) + len(marker):]
    lines = []
    for line in body.split("\n"):
        if line.strip() and not line.startswith(" " * 12):
            break
        lines.append(line[12:] if line.startswith(" " * 12) else line)
    script = "\n".join(lines)

    def one(match):
        name = match.group(1)
        if name.startswith("!"):
            # ${!foo} is !Sub's escape for a literal shell ${foo}.
            return "${" + name[1:] + "}"
        if name not in SUBS:
            raise AssertionError(f"unsubstituted template reference: {name}")
        return SUBS[name]

    return re.sub(r"\$\{([^}]+)\}", one, script)


def env_for(script):
    """The WS13_* names a script reads but does not set.

    !Sub used to interpolate these; now UserData exports them. A name a script
    reads and UserData never exports is silently empty on the node, so the two
    lists are checked against each other rather than assumed to agree.
    """
    return set(re.findall(r"\$\{?(WS13_[A-Z_]+)", script))


FAKE_AWS = r'''#!PYTHON
"""Just enough AWS for the fleet scripts: an S3 object store in a directory.

Every invocation is appended to $FAKE_CALLS, so a test can assert that a node
terminated with --should-decrement-desired-capacity, or did not.
"""
import json, os, sys, time

STATE = os.environ["FAKE_S3"]


def now():
    """Real time, plus the virtual clock when a test has turned it on.

    A claim written after a wait has to look as recent as the wait made it;
    stamping it with real time while `date +%s` reads the virtual clock would
    make every fresh claim look ClaimStaleSeconds old the moment a loop slept.
    """
    if os.environ.get("FAKE_VIRTUAL_CLOCK") != "1":
        return time.time()
    try:
        with open(os.environ["FAKE_CLOCK"]) as handle:
            return time.time() + int(handle.read().strip() or 0)
    except (KeyError, OSError, ValueError):
        return time.time()


with open(os.environ["FAKE_CALLS"], "a") as handle:
    handle.write(" ".join(sys.argv[1:]) + "\n")

argv = sys.argv[1:]


def opt(name, default=None):
    return argv[argv.index(name) + 1] if name in argv else default


def paths(key):
    body = os.path.join(STATE, key.replace("/", "__"))
    return body, body + ".meta"


def iso(epoch):
    return time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(epoch))


service = argv[0] if argv else ""
action = argv[1] if len(argv) > 1 else ""

if service == "s3api" and action == "put-object":
    body, meta = paths(opt("--key", ""))
    if "--if-none-match" in argv and os.path.exists(body):
        sys.stderr.write("PreconditionFailed\n")
        sys.exit(1)
    with open(body, "w") as handle:
        handle.write(opt("--body", "") or "")
    fields = dict(part.split("=", 1)
                  for part in (opt("--metadata") or "").split(",") if part)
    with open(meta, "w") as handle:
        json.dump({"state": fields.get("state", "None"),
                   "node": fields.get("node", ""),
                   "modified": now()}, handle)
    sys.exit(0)

if service == "s3api" and action == "head-object":
    body, meta = paths(opt("--key", ""))
    if not os.path.exists(body):
        sys.stderr.write("404\n")
        sys.exit(255)
    with open(meta) as handle:
        blob = json.load(handle)
    stamp = blob["modified"]
    # "alive" models a holder that is still refreshing its claim every 300 s:
    # its LastModified is whatever the clock says right now, so it never goes
    # stale however long the reader waits. A fixed timestamp cannot model that
    # once a test turns the virtual clock on -- the claim ages out under the
    # waiting node and gets adopted, which is the opposite of the case.
    if stamp == "alive":
        stamp = now()
    # Any other non-numeric value is emitted verbatim, so a test can present
    # the LastModified that fails to parse.
    print("%s\t%s" % (iso(stamp) if isinstance(stamp, (int, float))
                      else stamp, blob["state"]))
    sys.exit(0)

if service == "s3api" and action == "delete-object":
    for path in paths(opt("--key", "")):
        if os.path.exists(path):
            os.unlink(path)
    sys.exit(0)

# Everything else (s3 cp, cloudwatch, sqs, autoscaling) is recorded above and
# succeeds: none of it is what these tests are about.
sys.exit(0)
'''

FAKE_DATE = r'''#!PYTHON
"""GNU date, to the extent claim_slot.sh uses it.

`date -d <iso> +%s` is how a claim's age is read, and BSD date -- which is
what a macOS test host has -- rejects -d outright. Without this the age
always parses as epoch 0, every claim looks infinitely stale, and the branch
under test never runs.

`date +%s` adds the virtual clock when FAKE_VIRTUAL_CLOCK=1 -- real time plus
every second the sleep stub has been asked for -- so a poll loop bounded by a
deadline terminates in the iterations it would really take, instead of
spinning until the real deadline arrives. Off by default, because the tests
written before it depend on claim ages moving with real time.
"""
import calendar, os, subprocess, sys, time


def offset():
    if os.environ.get("FAKE_VIRTUAL_CLOCK") != "1":
        return 0
    try:
        with open(os.environ["FAKE_CLOCK"]) as handle:
            return int(handle.read().strip() or 0)
    except (KeyError, OSError, ValueError):
        return 0


argv = sys.argv[1:]
if "-d" in argv:
    value = argv[argv.index("-d") + 1]
    fmt = argv[-1]
    stamp = value.replace("+00:00", "").replace("Z", "").strip()
    try:
        parsed = time.strptime(stamp, "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        sys.exit(1)
    if fmt == "+%s":
        print(int(calendar.timegm(parsed)))
        sys.exit(0)
    sys.exit(1)
if argv and argv[-1] == "+%s":
    print(int(time.time()) + offset())
    sys.exit(0)
sys.exit(subprocess.call(["/bin/date"] + argv))
'''

FAKE_SLEEP = r'''#!/bin/sh
# Records the requested delay and returns at once, so a backoff schedule is
# asserted rather than waited out -- but it ADVANCES THE CLOCK by that delay
# first. Both wait loops in this template are `until a deadline, polling every
# 60 s`, so a sleep that returned instantly without moving `date +%s` turned
# each of them into a tight spin for the whole wall-clock wait: the adopt loop
# hung these tests for its full ClaimStaleSeconds+300. A virtual clock is also
# the more faithful stub -- it is what makes "this node waited 2100 s" an
# assertion rather than an inference.
echo "$1" >> "$FAKE_SLEEPS"
if [ "${FAKE_VIRTUAL_CLOCK:-0}" = 1 ]; then
  offset=$(cat "$FAKE_CLOCK" 2>/dev/null || echo 0)
  echo $((offset + $1)) > "$FAKE_CLOCK"
fi
exit 0
'''

FAKE_CURL = r'''#!/bin/sh
# IMDS: a token for the PUT, an instance id for everything else.
case "$*" in
  *api/token*) echo "fake-imds-token" ;;
  *instance-id*) echo "$FAKE_INSTANCE_ID" ;;
  *target-lifecycle-state*)
    cat "$FAKE_LIFECYCLE" 2>/dev/null || echo "InService" ;;
  *placement/region*) echo "us-west-2" ;;
  *) echo "" ;;
esac
exit 0
'''

# Exit codes read one per line from $FAKE_RC; the file is the script.
FAKE_PYTHON = r'''#!/bin/sh
echo "$*" >> "$FAKE_PY_CALLS"
if [ ! -f "$FAKE_RC" ]; then exit 0; fi
code=$(head -1 "$FAKE_RC")
tail -n +2 "$FAKE_RC" > "$FAKE_RC.next" && mv "$FAKE_RC.next" "$FAKE_RC"
[ -n "$code" ] || code=0
exit "$code"
'''


class Sandbox:
    """A relocated /opt/ws13 with stub binaries in front of PATH."""

    def __init__(self, scripts, stub=()):
        self.dir = Path(tempfile.mkdtemp(prefix="ws13-fleet-"))
        self.opt = self.dir / "opt" / "ws13"
        self.log = self.dir / "var" / "log"
        self.bin = self.dir / "bin"
        self.state = self.dir / "s3"
        for path in (self.opt / "status", self.log, self.bin, self.state):
            path.mkdir(parents=True, exist_ok=True)
        self.calls = self.dir / "calls.log"
        self.sleeps = self.dir / "sleeps.log"
        # Seconds the sleep stub has been asked for. `date +%s`
        # adds it, so a deadline loop advances instead of spinning.
        self.clock = self.dir / "clock.txt"
        self.py_calls = self.dir / "python.log"
        self.rc_script = self.dir / "rc.txt"
        self.lifecycle = self.dir / "lifecycle.txt"
        self.calls.write_text("")
        self.sleeps.write_text("")
        self.clock.write_text("0")
        self.virtual_clock = False
        self.py_calls.write_text("")
        self.lifecycle.write_text("InService")
        for name, body in scripts.items():
            if name in stub:
                continue
            self.write(self.opt / name, self.relocate(body))
        for name, body in (("aws", FAKE_AWS), ("date", FAKE_DATE),
                           ("sleep", FAKE_SLEEP), ("curl", FAKE_CURL),
                           ("python3", FAKE_PYTHON)):
            # The interpreter by absolute path: `python3` on this PATH is the
            # stub standing in for the confidence pass, so a
            # `#!/usr/bin/env python3` stub would end up running itself.
            self.write(self.bin / name,
                       body.replace("#!PYTHON", "#!" + sys.executable))

    def relocate(self, body):
        return (body.replace("/opt/ws13", str(self.opt))
                    .replace("/var/log", str(self.log)))

    @staticmethod
    def write(path, body):
        path.write_text(body)
        path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP)

    def env(self, **extra):
        env = dict(os.environ)
        env["PATH"] = f"{self.bin}:{env['PATH']}"
        # What UserData exports. !Sub used to bake these values into the
        # heredocs; now the scripts read them from the environment, so the
        # sandbox has to be the environment. FLEET_ENV is derived from SUBS,
        # not restated, so the harness and the template cannot drift.
        env.update(FLEET_ENV)
        env.update({"FAKE_S3": str(self.state), "FAKE_CALLS": str(self.calls),
                    "FAKE_SLEEPS": str(self.sleeps),
                    "FAKE_CLOCK": str(self.clock),
                    "FAKE_VIRTUAL_CLOCK":
                        "1" if self.virtual_clock else "0",
                    "FAKE_PY_CALLS": str(self.py_calls),
                    "FAKE_RC": str(self.rc_script),
                    "FAKE_LIFECYCLE": str(self.lifecycle),
                    "FAKE_INSTANCE_ID": "i-fake"})
        env.update({k: str(v) for k, v in extra.items()})
        return env

    def run(self, script, *args, **extra):
        return subprocess.run(
            ["bash", str(self.opt / script), *args], cwd=str(self.opt),
            env=self.env(**extra), capture_output=True, text=True, timeout=120)

    def claim_as(self, node):
        (self.opt / "claim").write_text(node)
        return self.run("claim_slot.sh", "claim")

    def slot_meta(self, slot):
        key = f"ws13/fleet/confidence/claims/shard-{slot}".replace("/", "__")
        path = self.state / (key + ".meta")
        return json.loads(path.read_text()) if path.exists() else None

    def write_slot(self, slot, state, node="i-other", age=0, alive=False):
        """Put a claim in the store. `alive` means its holder keeps refreshing.

        A number for `age` is a claim last touched that many seconds ago and
        then never again -- the abandoned case. `alive=True` is the live one:
        the stub renders its LastModified as the current time on every read,
        so it never becomes stale no matter how long a waiting node polls.
        """
        key = f"ws13/fleet/confidence/claims/shard-{slot}".replace("/", "__")
        (self.state / key).write_text(node)
        (self.state / (key + ".meta")).write_text(json.dumps(
            {"state": state, "node": node,
             "modified": "alive" if alive else time.time() - age}))

    def called(self, needle):
        return [line for line in self.calls.read_text().splitlines()
                if needle in line]

    def cleanup(self):
        shutil.rmtree(self.dir, ignore_errors=True)


class FleetTemplateTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.user_data = render_user_data()
        cls.scripts = fleet_scripts()

    def sandbox(self, stub=()):
        box = Sandbox(self.scripts, stub=stub)
        self.addCleanup(box.cleanup)
        return box


class EmbeddedScriptTests(FleetTemplateTestCase):
    """These are programs, and one that does not parse fails on a node hours
    after the stack says CREATE_COMPLETE."""

    EXPECTED = SCRIPT_NAMES

    def test_every_expected_script_is_written(self):
        self.assertEqual(tuple(sorted(self.scripts)), tuple(sorted(
            self.EXPECTED)))

    def test_each_one_parses_as_shell(self):
        box = self.sandbox()
        for name in self.EXPECTED:
            with self.subTest(script=name):
                done = subprocess.run(["bash", "-n", str(box.opt / name)],
                                      capture_output=True, text=True)
                self.assertEqual(done.returncode, 0, done.stderr)

    def test_the_user_data_itself_parses_as_shell(self):
        box = self.sandbox()
        path = box.dir / "user-data.sh"
        path.write_text(self.user_data)
        done = subprocess.run(["bash", "-n", str(path)], capture_output=True,
                              text=True)
        self.assertEqual(done.returncode, 0, done.stderr)


class SlotClaimTests(FleetTemplateTestCase):
    """The claim protocol, run for real against a conditional-put store."""

    def test_each_node_wins_a_distinct_slot(self):
        box = self.sandbox()
        won = [box.claim_as(f"i-{n}").stdout.strip() for n in range(SLOTS)]
        self.assertEqual(sorted(won), [str(i) for i in range(SLOTS)])
        self.assertEqual(len(set(won)), SLOTS, "two nodes shared a slot")

    def test_a_node_arriving_with_every_slot_fresh_claims_nothing(self):
        box = self.sandbox()
        for slot in range(SLOTS):
            box.write_slot(slot, "running", age=60)
        done = box.claim_as("i-late")
        self.assertEqual(done.returncode, 1)
        self.assertEqual(done.stdout.strip(), "")

    def test_a_claim_past_the_staleness_window_is_taken_over(self):
        box = self.sandbox()
        for slot in range(SLOTS):
            box.write_slot(slot, "running", node="i-dead", age=60)
        box.write_slot(2, "running", node="i-dead", age=STALE + 60)
        done = box.claim_as("i-replacement")
        self.assertEqual(done.stdout.strip(), "2")
        self.assertEqual(box.slot_meta(2)["node"], "i-replacement")

    def test_a_completed_slot_is_never_taken_over_however_old(self):
        # Marking complete is what stops a node booting later from paying to
        # re-walk a shard that is already measured.
        box = self.sandbox()
        for slot in range(SLOTS):
            box.write_slot(slot, "complete", node="i-done",
                           age=STALE * 100)
        self.assertEqual(box.claim_as("i-late").returncode, 1)
        self.assertEqual(box.slot_meta(0)["node"], "i-done")

    def test_an_unreadable_last_modified_fails_toward_taking_over(self):
        # The deliberate direction to fail in: a duplicated shard costs CPU
        # and rewrites nothing, an abandoned one is pages nothing reports.
        box = self.sandbox()
        for slot in range(SLOTS):
            box.write_slot(slot, "running", node="i-dead", age=60)
        key = "ws13__fleet__confidence__claims__shard-1.meta"
        (box.state / key).write_text(json.dumps(
            {"state": "running", "node": "i-dead",
             "modified": "an-unparseable-timestamp"}))
        self.assertEqual(box.claim_as("i-replacement").stdout.strip(), "1")

    def test_incomplete_counts_a_slot_nobody_ever_claimed(self):
        # The silent hole itself: an unclaimed slot has no object in S3, so a
        # scan that only looked at existing claims reports the run finished.
        box = self.sandbox()
        for slot in range(SLOTS - 1):
            box.write_slot(slot, "complete")
        done = box.run("claim_slot.sh", "incomplete")
        self.assertEqual(done.stdout.strip(), "1")

    def test_incomplete_is_zero_only_when_every_slot_is_complete(self):
        box = self.sandbox()
        for slot in range(SLOTS):
            box.write_slot(slot, "complete")
        self.assertEqual(box.run("claim_slot.sh", "incomplete").stdout.strip(),
                         "0")
        box.write_slot(1, "running")
        self.assertEqual(box.run("claim_slot.sh", "incomplete").stdout.strip(),
                         "1")

    def test_an_unknown_verb_is_refused_rather_than_guessed(self):
        box = self.sandbox()
        self.assertEqual(box.run("claim_slot.sh", "finish").returncode, 2)


class ClaimWaitTests(FleetTemplateTestCase):
    """Blocker 1: a replacement must not decrement the group it was born to
    rescue.

    The UserData preamble is not runnable in a test (dnf, docker, Secrets
    Manager), so the wait is asserted over the rendered text -- but over its
    STRUCTURE: what the loop calls, what bounds it, and where the decrement
    sits relative to it.
    """

    def setUp(self):
        # The branch lives in node_boot.sh now, not in UserData: the
        # heredocs moved into the bundle when the rendered UserData turned
        # out to be ~30,000 bytes against EC2's 16,384-byte limit.
        boot = self.scripts["node_boot.sh"]
        self.branch = boot[
            boot.index('if [ "$WS13_MODE" = confidence ]; then'):
            boot.index("export WS13_NODE_SLOT=$SLOT")]

    def test_the_claim_is_retried_in_a_loop_not_attempted_once(self):
        self.assertIn("/opt/ws13/claim_slot.sh claim", self.branch)
        loop = self.branch[self.branch.index("while : ; do"):]
        self.assertIn("claim_slot.sh claim", loop)
        self.assertIn("sleep 60", loop)

    def test_the_wait_outlasts_the_staleness_window(self):
        # A claim refreshed 30 s ago and one abandoned 30 s ago are
        # indistinguishable until ClaimStaleSeconds has passed; the extra 300 s
        # is one refresh interval, so anything still unfinished afterwards has
        # demonstrably been refreshed by a live node.
        self.assertIn("CLAIM_WAIT=$(( ${WS13_CLAIM_STALE} + 300 ))",
                      self.branch)
        self.assertIn("CLAIM_DEADLINE=$(( $(date +%s) + CLAIM_WAIT ))",
                      self.branch)

    def test_an_all_complete_run_breaks_out_at_once(self):
        # Waiting 1800 s to retire a node into a finished run would be its own
        # bug: every slot complete means there is nothing to reclaim.
        self.assertIn("claim_slot.sh incomplete", self.branch)
        self.assertRegex(self.branch,
                         r'if \[ "\$unfinished" -eq 0 \]; then')

    def test_the_decrement_happens_only_after_the_wait(self):
        wait = self.branch.index("CLAIM_DEADLINE=")
        loop_end = self.branch.index('if [ -z "$SLOT" ]; then',
                                     self.branch.index("while : ; do"))
        # The command, not the comment that explains why it moved.
        decrement = self.branch.index(
            "aws autoscaling terminate-instance-in-auto-scaling-group")
        self.assertLess(wait, loop_end)
        self.assertLess(loop_end, decrement,
                        "the group is decremented before the wait completes")
        self.assertIn("--should-decrement-desired-capacity",
                      self.branch[decrement:])

    def test_the_reason_for_retiring_is_logged_either_way(self):
        self.assertIn("every one of", self.branch)
        self.assertIn("surplus", self.branch)


class SweepLoopTests(FleetTemplateTestCase):
    """Blocker 2: exit 1 meant two different things, and neither backed off."""

    ENV = {"WS13_MODE": "confidence", "WS13_NODE_SLOT": "0",
           "WS13_WORKERS_PER_NODE": "2", "WS13_SWEEP_DOCS": "25",
           "WS13_SWEEP_MAX_SECONDS": "86400"}

    def sweep(self, codes, **extra):
        box = self.sandbox()
        box.rc_script.write_text("\n".join(str(c) for c in codes) + "\n")
        env = dict(self.ENV)
        env["WS13_DRAIN_FILE"] = str(box.opt / "drain")
        env.update(extra)
        done = box.run("run_worker.sh", "1", **env)
        self.assertEqual(done.returncode, 0, done.stderr)
        status = (box.opt / "status" / "1").read_text().strip()
        sweeps = len([line for line in box.py_calls.read_text().splitlines()
                      if "ws13_confidence_pass.py" in line])
        naps = [int(n) for n in box.sleeps.read_text().split()]
        return status, sweeps, naps, box

    def test_work_remaining_sweeps_again_immediately(self):
        status, sweeps, naps, _box = self.sweep([10, 10, 0])
        self.assertEqual(status, "0")
        self.assertEqual(sweeps, 3)
        self.assertEqual(naps, [], "progress must not be made to wait")

    def test_a_measured_shard_stops_after_one_sweep(self):
        status, sweeps, _naps, _box = self.sweep([0])
        self.assertEqual((status, sweeps), ("0", 1))

    def test_a_stalled_sweep_backs_off_geometrically(self):
        _status, _sweeps, naps, _box = self.sweep([11, 11, 11, 0])
        self.assertEqual(naps, [60, 120, 240])

    def test_a_shard_that_never_progresses_gives_up_and_reports_failure(self):
        # It must NOT sweep to the 24 h ceiling: a wedged shard has to reach
        # the node agent as a failure so the node is held and the alarm fires.
        status, sweeps, naps, box = self.sweep([11] * 20)
        self.assertEqual(status, "11")
        self.assertEqual(sweeps, 6)
        self.assertEqual(len(naps), 5)
        self.assertIn("measured nothing in 6 consecutive sweeps",
                      (box.log / "ws13-confidence-1.log").read_text())

    def test_progress_resets_the_backoff(self):
        # A transient stall in the middle of a long run must not leave the
        # shard sweeping on a 15-minute cadence for the rest of it.
        _status, _sweeps, naps, _box = self.sweep([11, 11, 10, 11, 0])
        self.assertEqual(naps, [60, 120, 60])

    def test_an_uncaught_exception_is_no_longer_read_as_work_remaining(self):
        # The overloaded 1. Three tries, backed off, then failed -- not swept
        # for 24 h.
        status, sweeps, naps, box = self.sweep([1] * 20)
        self.assertEqual(status, "1")
        self.assertEqual(sweeps, 3)
        self.assertEqual(naps, [60, 120])
        self.assertIn("unhandled",
                      (box.log / "ws13-confidence-1.log").read_text())

    def test_a_recoverable_fault_does_not_fail_the_worker(self):
        # A torn database connection presents as exit 1, so a couple of
        # retries are allowed; what is not allowed is a hot loop.
        status, sweeps, naps, _box = self.sweep([1, 10, 0])
        self.assertEqual((status, sweeps, naps), ("0", 3, [60]))

    def test_shard_arithmetic_and_renderer_failures_reach_the_agent(self):
        for code in (2, 3):
            with self.subTest(code=code):
                status, sweeps, _naps, _box = self.sweep([code, 0])
                self.assertEqual((status, sweeps), (str(code), 1))

    def test_a_drain_ends_the_sweep_cleanly(self):
        # Being asked to leave is not a failure; the agent then releases the
        # claim so another node re-measures the shard.
        box = self.sandbox()
        box.rc_script.write_text("10\n0\n")
        (box.opt / "drain").touch()
        env = dict(self.ENV, WS13_DRAIN_FILE=str(box.opt / "drain"))
        box.run("run_worker.sh", "1", **env)
        self.assertEqual((box.opt / "status" / "1").read_text().strip(), "0")
        self.assertEqual(box.py_calls.read_text().strip(), "")

    def test_the_shard_index_is_the_slot_times_the_worker_count(self):
        box = self.sandbox()
        box.rc_script.write_text("0\n")
        env = dict(self.ENV, WS13_NODE_SLOT="3",
                   WS13_DRAIN_FILE=str(box.opt / "drain"))
        box.run("run_worker.sh", "2", **env)
        text = (box.log / "ws13-confidence-2.log").read_text()
        self.assertNotIn("Traceback", text)
        # slot 3, workers-per-node 2, process 2 -> shard 7.
        self.assertIn("WS13_SHARD", self.scripts["run_worker.sh"])
        self.assertIn("SHARD=$(( WS13_NODE_SLOT * WS13_WORKERS_PER_NODE"
                      " + $1 - 1 ))", self.scripts["run_worker.sh"])

    def test_the_ceiling_still_bounds_a_shard_that_keeps_progressing(self):
        status, sweeps, _naps, _box = self.sweep(
            [10] * 5, WS13_SWEEP_MAX_SECONDS="0")
        self.assertEqual(sweeps, 0)
        self.assertEqual(status, "10", "a ceiling stop is not a clean finish")


class StartWorkersTests(FleetTemplateTestCase):
    def test_it_records_one_pid_per_worker(self):
        box = self.sandbox()
        done = box.run("start_workers.sh", WS13_MODE="ocr")
        self.assertEqual(done.returncode, 0, done.stderr)
        pids = (box.opt / "worker.pids").read_text().split()
        self.assertEqual(len(pids), int(SUBS["WorkersPerNode"]))

    def test_it_clears_the_previous_generations_statuses(self):
        # node_agent.sh reads "every worker wrote a status" as the completion
        # condition, so a second generation starting with the first
        # generation's status files would be seen as finished before it ran.
        box = self.sandbox()
        (box.opt / "status" / "1").write_text("0")
        (box.opt / "status" / "2").write_text("0")
        box.run("start_workers.sh", WS13_MODE="ocr")
        time.sleep(0.2)
        stale = [p for p in (box.opt / "status").iterdir()
                 if p.read_text().strip() == "0"]
        self.assertLessEqual(len(stale), int(SUBS["WorkersPerNode"]))


class SlotCompletionTests(FleetTemplateTestCase):
    """Blocker 3: the group must not decrement to zero over an unmeasured
    shard.

    node_agent.sh is run to completion with start_workers.sh replaced by a
    recorder, so the adopt decision is exercised without launching workers.
    """

    # Stands in for start_workers.sh: records that it was called and leaves
    # the clean exit statuses a finished generation of workers would leave.
    RECORDER = ('#!/bin/sh\necho "started" >> "$FAKE_STARTS"\n'
                'echo 0 > STATUSDIR/1\necho 0 > STATUSDIR/2\n')

    def agent(self, slots, statuses=("0", "0"), slot="0", mode="confidence",
              recorder=None, terminated=False):
        """Run node_agent.sh to completion over a given claim state.

        `slots` is {index: (state, node, age_seconds)}; whatever it does not
        name has never been claimed. start_workers.sh is replaced by a
        recorder, so an adopted generation reports its statuses without a
        worker ever starting.
        """
        box = self.sandbox(stub=("start_workers.sh",))
        # The adopt step polls every 60 s until ClaimStaleSeconds + 300, the
        # same wait the boot-time claim loop makes. With sleep stubbed to
        # return at once and `date` on the real clock that is a tight spin for
        # 2,100 real seconds, so this is the one place the virtual clock has
        # to be on: it makes the loop take the ~35 iterations it would really
        # take, and makes "waited out the window" an assertion.
        box.virtual_clock = True
        starts = box.dir / "starts.log"
        starts.write_text("")
        Sandbox.write(box.opt / "start_workers.sh",
                      (recorder or self.RECORDER).replace(
                          "STATUSDIR", str(box.opt / "status")))
        for index, code in enumerate(statuses, 1):
            (box.opt / "status" / str(index)).write_text(code)
        (box.opt / "claim").write_text("i-fake")
        for index, spec in slots.items():
            state, node, age = spec
            box.write_slot(index, state, node=node,
                           age=0 if age == "alive" else age,
                           alive=age == "alive")
        if terminated:
            # A worker that exited non-zero puts the node into the 6 h
            # diagnosis hold, which is 72 iterations of a 300 s sleep. Asking
            # the group for the node back leaves that hold on its first pass;
            # everything these tests assert is published before it is entered.
            box.lifecycle.write_text("Terminated")
        # A shorter staleness window than production's 1800 s, because the
        # adopt wait polls WS13_CLAIM_STALE + 300 seconds of virtual clock at
        # 60 s a step and every step forks claim_slot.sh twice: at the real
        # default that is ~35 iterations and several hundred stub processes,
        # which is fine alone and times out when the whole suite is running.
        # The DURATION is what these tests assert, and it is asserted against
        # this value -- which is only injectable at all because the parameter
        # now arrives as environment rather than being baked in by !Sub.
        env = {"WS13_MODE": mode, "FAKE_STARTS": str(starts),
               "WS13_CLAIM_STALE": str(AGENT_STALE)}
        if mode == "confidence":
            env["WS13_NODE_SLOT"] = slot
            env["WS13_CLAIM_KEY"] = (
                f"ws13/fleet/confidence/claims/shard-{slot}")
        box.run("node_agent.sh", str(len(statuses)), **env)
        return box, starts

    def test_a_finished_node_adopts_a_slot_nobody_claimed(self):
        # Slots 1-3 have no object in S3 at all. That is the silent hole: the
        # group would have decremented to zero over three unmeasured shards.
        box, starts = self.agent({0: ("running", "i-fake", 1)})
        self.assertIn("started", starts.read_text(),
                      "the node retired instead of adopting an open slot")
        self.assertEqual(box.slot_meta(1)["node"], "i-fake")

    def test_it_adopts_a_slot_whose_holder_stopped_refreshing(self):
        box, starts = self.agent({
            0: ("running", "i-fake", 1),
            1: ("running", "i-dead", STALE + 120),
            2: ("running", "i-alive", 60), 3: ("running", "i-alive", 60)})
        self.assertEqual(box.slot_meta(1)["node"], "i-fake")
        self.assertIn("started", starts.read_text())

    def test_a_node_retires_only_when_no_slot_is_claimable(self):
        box, starts = self.agent(
            {slot: ("complete", "i-done", 10) for slot in range(SLOTS)})
        self.assertEqual(starts.read_text().strip(), "")
        self.assertTrue(box.called("--should-decrement-desired-capacity"))

    def test_a_live_holder_is_not_robbed_and_the_node_retires(self):
        # Every other slot is held by a node that is demonstrably alive, so
        # this node really is surplus.
        box, starts = self.agent({
            0: ("running", "i-fake", 1),
            1: ("running", "i-alive", "alive"),
            2: ("running", "i-alive", "alive"),
            3: ("running", "i-alive", "alive")})
        self.assertEqual(starts.read_text().strip(), "",
                         "a slot a live node is still refreshing was adopted")
        self.assertEqual(box.slot_meta(1)["node"], "i-alive")
        self.assertTrue(box.called("--should-decrement-desired-capacity"))
        # ...and it did not decrement until it had waited the window out,
        # which is the whole difference between this and the blocker.
        waited = sum(int(v) for v in box.sleeps.read_text().split())
        self.assertGreaterEqual(waited, AGENT_STALE + 300)

    def test_the_outstanding_slot_count_is_published_before_retiring(self):
        # This node's workers failed, so its own slot is released and stays
        # outstanding -- and the number says so rather than the empty group
        # implying otherwise.
        box, _starts = self.agent(
            {0: ("running", "i-fake", 1),
             1: ("complete", "i-done", 10), 2: ("complete", "i-done", 10),
             3: ("complete", "i-done", 10)},
            statuses=("1", "1"), terminated=True)
        published = box.called("SlotsIncomplete")
        self.assertTrue(published, "nothing reported the outstanding slots")
        self.assertIn("--value 1", published[0])

    def test_a_completed_run_publishes_zero_outstanding(self):
        box, _starts = self.agent(
            {slot: ("complete", "i-done", 10) for slot in range(SLOTS)})
        published = box.called("SlotsIncomplete")
        self.assertTrue(published)
        self.assertIn("--value 0", published[0])

    def test_a_failed_node_releases_its_slot_and_does_not_adopt(self):
        # Its workers died; taking a second shard would only lose that one too.
        box, starts = self.agent({0: ("running", "i-fake", 1)},
                                 statuses=("3", "3"), terminated=True)
        self.assertIsNone(box.slot_meta(0), "an unfinished slot was not freed")
        self.assertEqual(starts.read_text().strip(), "")

    def test_an_ocr_node_is_untouched_by_any_of_this(self):
        # FleetMode ocr is today's fleet and must behave exactly as it did:
        # one generation of workers, no claim, no adopt.
        box, starts = self.agent({}, mode="ocr")
        self.assertEqual(starts.read_text().strip(), "")
        self.assertFalse(box.called("SlotsIncomplete"))
        self.assertFalse(box.called("s3api put-object"))
        self.assertTrue(box.called("--should-decrement-desired-capacity"))


class VerificationPointerTests(FleetTemplateTestCase):
    """The claim objects are bookkeeping; ws13_pages is the ground truth.

    A node that died before it ever claimed a slot leaves nothing in S3 at
    all, so the assertion an operator runs at the end has to be the one that
    reads the database.
    """

    def test_the_agent_names_the_command_that_actually_proves_completion(self):
        agent = self.scripts["node_agent.sh"]
        self.assertIn("ws13_confidence_pass.py --verify-complete", agent)

    def test_the_shard_count_it_prints_is_the_one_the_processes_use(self):
        # WS13_SHARD_COUNT is ConfidenceNodeSlots x WorkersPerNode; verifying
        # against any other denominator checks a different partition.
        agent = self.scripts["node_agent.sh"]
        self.assertIn("--shards $(( ${WS13_NODE_SLOTS} * "
                      "${WS13_WORKERS_PER_NODE} ))", agent)


# EC2 refuses instance user data over this, and CreateLaunchTemplateVersion
# refuses with it, so the stack cannot be created at all.
EC2_USER_DATA_LIMIT = 16384


class UserDataSizeTests(unittest.TestCase):
    """The UserData has to fit in what EC2 will accept.

    This is not a style limit. CreateLaunchTemplateVersion refuses anything
    larger, so `aws cloudformation deploy` fails on the LaunchTemplate with
    "User data is limited to 16384 bytes" and rolls the stack back before a
    node boots -- in BOTH modes, because the claim script was written in both.

    Measured across the history of this file, with the declared defaults:

        f335c8c   8,599 B   ok      <- the fleet that OCR'd the corpus
        c1accaf  20,689 B   OVER    <- FleetMode: confidence arrives
        19dea85  20,689 B   OVER
        (before the shell moved out)  ~30,000 B   OVER

    So `FleetMode: confidence` was never deployable, from the commit that
    introduced it -- consistent with the pass never having run, but stated
    nowhere, while every other test here passed over shell that could not
    reach a node. The five scripts now ship in bundle.tar.gz and UserData is
    the ~40 lines that fetch it.
    """

    def setUp(self):
        self.user_data = render_user_data()

    def test_the_user_data_fits_in_what_ec2_accepts(self):
        size = len(self.user_data.encode())
        self.assertLessEqual(
            size, EC2_USER_DATA_LIMIT,
            f"UserData is {size:,} bytes against EC2's "
            f"{EC2_USER_DATA_LIMIT:,}: the LaunchTemplate cannot be created. "
            f"Anything this large belongs in bundle.tar.gz, not here")

    def test_it_keeps_room_to_grow(self):
        # Half the limit is the line: past it, one more paragraph of comment
        # is a deploy failure, which is how this got to 30,000 bytes the
        # first time.
        size = len(self.user_data.encode())
        self.assertLess(size, EC2_USER_DATA_LIMIT // 2,
                        f"UserData is {size:,} bytes, over half the "
                        f"{EC2_USER_DATA_LIMIT:,}-byte limit. Move shell into "
                        f"infra/fleet/ and the bundle rather than growing it")

    def test_every_name_the_scripts_read_is_exported_by_user_data(self):
        """A parameter that stops reaching the node is silently empty.

        !Sub used to interpolate these values into the shell, so a missing one
        was a template error at deploy time. Now they travel as environment,
        and a name the scripts read that UserData never exports is the empty
        string on the node -- `[ "$WS13_NODE_SLOTS" -eq 0 ]` and friends, at
        boot, in a script nobody is watching.
        """
        # Every NAME= on an `export` line, not just the first: UserData
        # exports WS13_BUCKET and WS13_QUEUE_URL on one line.
        exported = set()
        for line in self.user_data.split("\n"):
            if line.strip().startswith("export "):
                exported |= set(re.findall(r"(WS13_[A-Z_]+)=", line))
        read = set()
        for name, body in fleet_scripts().items():
            read |= env_for(body)
        # Names the scripts set themselves, so UserData need not.
        set_by_scripts = set()
        for body in fleet_scripts().values():
            set_by_scripts |= set(
                re.findall(r"^\s*(?:export )?(WS13_[A-Z_]+)=", body, re.M))
        missing = sorted(read - exported - set_by_scripts)
        self.assertEqual(missing, [],
                         f"the scripts read {missing}, which UserData never "
                         f"exports: empty on the node")

    def test_the_shell_it_no_longer_carries_is_in_the_bundle(self):
        """UserData execs node_boot.sh, so the bundle has to contain it.

        Slimming the template and forgetting to ship what was cut is the one
        way this refactor fails silently: the stack creates, the node boots,
        and it dies on a missing file with a claimed shard slot in hand.
        """
        sys.path.insert(0, str(ROOT / "tools"))
        import build_ws13_bundle as bundle

        shipped = {name for name, _relative, _why in bundle.MEMBERS}
        for name in SCRIPT_NAMES:
            self.assertIn(name, shipped)
        self.assertIn("/opt/ws13/node_boot.sh", self.user_data)


if __name__ == "__main__":
    unittest.main()
