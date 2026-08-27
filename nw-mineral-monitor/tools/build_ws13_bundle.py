#!/usr/bin/env python3
"""Build ws13/fleet/bundle.tar.gz -- the code every WS13 node actually runs.

There was no builder. The bundle in S3 was assembled by hand, and the last
hand-assembly is dated 2026-08-24, before the worker and fleet fixes were
written. That is why "rebuild and upload the worker bundle" is step 0 of the
operator sequence in WS13-RETRIEVAL.md: the ASG is at 0 today, so nothing is
broken by the stale bundle right now, and the next scale-up runs the old code.

Two things go wrong when a bundle is assembled by hand, and both have already
happened here:

  * A FILE IS FORGOTTEN. pipelines/ws13_seed.py imports ws13_migrate and
    ws13_backfill_provenance as flat siblings, and a bundle built without them
    died on the seeding node with a bare ModuleNotFoundError -- which is why
    ws13_seed.bundle_files() exists at all. This script does not trust a
    hand-maintained list either: it parses every member with `ast`, collects
    each `import ws13_*` / `from ws13_* import ...`, and refuses to build if
    any of them is missing from MEMBERS. The list is closed under import or
    there is no archive. Two more lists have to agree with it and are read
    rather than restated: ws13_seed.BUNDLE_FILES, whose absence aborts the
    seeding node by name, and every `python3 ws13_*.py` the fleet invokes,
    read out of infra/ws13_fleet.yaml and out of the bundled *.sh -- an entry
    point added there and not here is a node that boots into "can't open
    file", in confidence mode while holding a claimed shard slot.
  * NOBODY KNOWS WHAT A NODE IS RUNNING. The fleet downloads a FIXED key,
    ws13/fleet/bundle.tar.gz, at boot. That is correct -- unlike a Lambda's
    code, this is fetched per instance, so a stable key is what makes a
    scale-up pick up the current code -- but it also means the key says
    nothing about the contents, and a node that booted before an upload runs
    the previous bundle with no record of the fact. So the archive carries
    bundle_manifest.json: the sha256 of every member and of the archive's own
    payload, untarred to /opt/ws13/bundle_manifest.json alongside the code.
    `cat` it on a node and the question "is this node running the fix?" has an
    answer that is not an inference from a LaunchTime.

The archive is BYTE-REPRODUCIBLE. Same sources in, same sha256 out: members
are sorted, every mtime is pinned to the epoch, uid/gid/uname/gname are
cleared, modes are normalised, and the gzip header carries no timestamp.
Reproducibility is not tidiness here -- it is the only way to answer "is the
object in S3 the bundle these sources build?" without downloading and
diffing it, which from a laptop is not possible anyway: the fleet bucket is
in an account this machine can read but must not write.

WHAT THIS SCRIPT WILL NOT DO. It will not upload. --upload prints the exact
`aws s3 cp` line and stops.

Not because it cannot: this bucket IS writable from a machine holding the
project credentials, and the 2026-08-27 bundle was published exactly that way,
by hand, from the line this prints. An earlier version of this comment claimed
the local permission classifier refused the write, which was inherited from
the handoff and never tested -- a restated claim, in a builder whose whole
purpose is to stop restated claims, and it is corrected here rather than
quietly deleted.

The real reason is that BUILDING and PUBLISHING are different decisions and a
build must not make the second one as a side effect of the first. Between them
sit the two checks that make the artifact worth having: --verify against these
sources, and reading the digest. A builder that uploaded on success would run
both of those AFTER the fleet could already download the result. Publishing is
one `aws s3 cp`, and it should be typed deliberately, by someone who has just
looked at the digest.

    # build to var/, verify the closure, print the digest
    tools/build_ws13_bundle.py

    # what the operator has to run, printed and not run
    tools/build_ws13_bundle.py --upload

    # check an archive that already exists (an S3 copy, a colleague's build)
    tools/build_ws13_bundle.py --verify var/ws13-fleet-bundle.tar.gz

    # prove reproducibility: two builds, one digest
    tools/build_ws13_bundle.py --output /tmp/a.tar.gz
    tools/build_ws13_bundle.py --output /tmp/b.tar.gz
"""

from __future__ import annotations

import argparse
import ast
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sys
import tarfile


HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

# The S3 location the fleet and the runner both hard-code. Named here so the
# printed upload line cannot drift from infra/ws13_fleet.yaml, which does
# `aws s3 cp s3://${BucketName}/ws13/fleet/bundle.tar.gz .` at boot.
BUCKET = os.environ.get('WS13_BUCKET', 'nw-mineral-monitor-730883236375')
KEY = 'ws13/fleet/bundle.tar.gz'
MANIFEST_NAME = 'bundle_manifest.json'
DEFAULT_OUTPUT = ROOT / 'var' / 'ws13-fleet-bundle.tar.gz'

# Every member is untarred FLAT into /opt/ws13 -- infra/ws13_fleet.yaml does
# `mkdir -p /opt/ws13 && cd /opt/ws13 && tar xzf bundle.tar.gz`, and both
# ws13_seed.bundle_files() and ws13_index_contract.resolve_query_lambda()
# resolve their siblings from beside themselves for exactly that reason. So
# the archive name is a basename, never a path, and two sources that share a
# basename would collide silently on extraction. assert_no_collisions() below
# is what stops that being discovered on a node.
#
# `why` is not decoration: it is the record of who runs each file, and the
# only thing that makes "can this be dropped?" answerable later.
MEMBERS = (
    # --- the fleet's own entry points -------------------------------------
    ('ws13_seed.py', 'pipelines/ws13_seed.py',
     'FleetMode: ocr, node 1 -- seeds the manifest and the work queue'),
    ('ws13_worker.py', 'pipelines/ws13_worker.py',
     'FleetMode: ocr -- WorkersPerNode of these per node'),
    ('ws13_confidence_pass.py', 'pipelines/ws13_confidence_pass.py',
     'FleetMode: confidence -- the sharded per-page measurement'),
    # --- imported as flat siblings by the above ---------------------------
    ('ws13_migrate.py', 'pipelines/ws13_migrate.py',
     'imported by ws13_seed; also the Phase C migration step over SSM'),
    ('ws13_migrations.sql', 'pipelines/ws13_migrations.sql',
     'read by ws13_seed and ws13_migrate; not importable, still required'),
    ('ws13_backfill_provenance.py', 'pipelines/ws13_backfill_provenance.py',
     'imported by ws13_seed and ws13_enqueue; Phase C provenance backfill'),
    # --- Phase A, on the in-VPC host over SSM ------------------------------
    ('ws13_build_ann_index.py', 'pipelines/ws13_build_ann_index.py',
     'Phase A -- pauses the backfill, builds ws13_chunks_titan_hnsw'),
    ('ws13_index_contract.py', 'pipelines/ws13_index_contract.py',
     'imported by ws13_build_ann_index and ws13_vector_bakeoff'),
    ('ws13_query_lambda.py', 'infra/ws13_query_lambda.py',
     'the single source of truth for the halfvec constants both of those '
     'read; the only member that is not from pipelines/'),
    # --- the remaining on-host steps of the operator sequence -------------
    ('ws13_rescue.py', 'pipelines/ws13_rescue.py',
     'the 7 failed documents; needs docker, so it runs on a node'),
    ('ws13_enqueue.py', 'pipelines/ws13_enqueue.py',
     'imported by ws13_rescue --requeue; also the DLQ requeue tool'),
    ('ws13_reap_stale.py', 'pipelines/ws13_reap_stale.py',
     'imported by ws13_rescue to reap rows stuck at running'),
    ('ws13_mine_id_map.py', 'pipelines/ws13_mine_id_map.py',
     'Phase D -- builds ws13_mine_id_map, which the retrieval path reads'),
    ('ws13_vector_bakeoff.py', 'pipelines/ws13_vector_bakeoff.py',
     'the bounded Titan/Cohere experiment that replaces the open-ended fill'),
    ('ws13_embed_backfill.py', 'pipelines/ws13_embed_backfill.py',
     'the process Phase A pauses; carried so a node can restart it after'),
    # --- the fleet's own shell ---------------------------------------------
    # These used to be heredocs inside infra/ws13_fleet.yaml's UserData, which
    # had grown to ~30,000 bytes against EC2's 16,384-byte limit -- so the
    # LaunchTemplate could not be created and FleetMode: confidence had never
    # been deployable. They ride here instead, which means the node's shell is
    # versioned with THIS ARCHIVE and not with the CloudFormation stack: a
    # stack update no longer changes what a node runs, and rebuilding and
    # uploading this bundle does.
    ('node_boot.sh', 'infra/fleet/node_boot.sh',
     'what UserData execs: claims a slot, seeds, starts workers and agent'),
    ('claim_slot.sh', 'infra/fleet/claim_slot.sh',
     'the S3 slot-claim protocol, used at boot and again on adopt'),
    ('run_worker.sh', 'infra/fleet/run_worker.sh',
     'one worker process, and the confidence sweep loop around it'),
    ('start_workers.sh', 'infra/fleet/start_workers.sh',
     'launches a generation of workers; run again when a slot is adopted'),
    ('node_agent.sh', 'infra/fleet/node_agent.sh',
     'the node lifecycle: watch, drain, release or adopt, retire or hold'),
)

# A node execs node_boot.sh; nothing chmods it before that but the UserData,
# so the archive has to carry the bit itself.
EXECUTABLE_SUFFIX = '.sh'

# A sibling import that resolves to something OUTSIDE this list is the defect
# this builder exists to prevent, with one exception that is not a defect:
# ws13_worker is imported by ws13_confidence_pass under the alias `worker`,
# and ws13_rescue imports ws13_worker inside a function. Both are members, so
# the exception is empty -- and it stays empty by assertion rather than by
# habit. If a module ever legitimately imports a ws13_* sibling that must NOT
# ship (a developer-only tool, say), add it here with the reason, and the
# closure check will stop failing for that one name only.
CLOSURE_EXEMPT: dict[str, str] = {}

# Pinned so two builds of the same sources agree. The epoch itself is
# arbitrary; that it never varies is the point.
EPOCH = 0
FILE_MODE = 0o644
EXEC_MODE = 0o755


def source_path(relative: str) -> Path:
    return ROOT / relative


def read_members() -> list[tuple[str, str, bytes]]:
    """[(archive_name, source_relpath, bytes)], in MEMBERS order.

    Missing sources are collected and reported together. Reporting the first
    one and exiting makes a stale MEMBERS list a sequence of rebuilds, one
    filename per run.
    """
    out, missing = [], []
    for name, relative, _why in MEMBERS:
        path = source_path(relative)
        if not path.is_file():
            missing.append(relative)
            continue
        out.append((name, relative, path.read_bytes()))
    if missing:
        sys.exit('missing source file(s), so the bundle would ship without '
                 'them:\n  ' + '\n  '.join(missing))
    return out


def assert_no_collisions() -> None:
    """No two members may extract to the same /opt/ws13 path.

    The archive is untarred flat, so a second `ws13_worker.py` from another
    directory would overwrite the first with no error, on the node, at boot.
    """
    seen: dict[str, str] = {}
    for name, relative, _why in MEMBERS:
        if name in seen:
            sys.exit(f'{name} is claimed by both {seen[name]} and {relative}; '
                     f'the archive extracts flat, so one would silently '
                     f'overwrite the other on the node')
        seen[name] = relative


def sibling_imports(source: bytes, filename: str) -> set[str]:
    """Every `ws13_*` module this file imports as a flat sibling.

    Parsed, not grepped: a grep matches the name in a docstring, in a comment
    and in a log message, and this repository's modules discuss each other by
    name constantly. Function-level imports count -- ws13_seed imports its two
    siblings inside main() precisely so bundle_files() can print a usable
    error first, and ws13_rescue imports ws13_worker inside a function too.
    Both are still hard requirements of the bundle.
    """
    tree = ast.parse(source, filename=filename)
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split('.')[0]
                if root.startswith('ws13_'):
                    found.add(root)
        elif isinstance(node, ast.ImportFrom):
            # level > 0 is a relative import, which cannot occur in a flat
            # directory of top-level modules; node.module is None for
            # `from . import x`, hence the guard.
            if node.level == 0 and node.module:
                root = node.module.split('.')[0]
                if root.startswith('ws13_'):
                    found.add(root)
    return found


def closure_problems(members: list[tuple[str, str, bytes]]) -> list[str]:
    """Names a member imports that the archive would not contain."""
    shipped = {name[:-3] for name, _relative, _data in members
               if name.endswith('.py')}
    problems = []
    for name, relative, data in members:
        if not name.endswith('.py'):
            continue
        for imported in sorted(sibling_imports(data, relative)):
            if imported in shipped or imported in CLOSURE_EXEMPT:
                continue
            problems.append(
                f'{relative} imports {imported}, which is not in MEMBERS: on '
                f'a node that is ModuleNotFoundError at run time. Add '
                f'{imported}.py to MEMBERS, or record it in CLOSURE_EXEMPT '
                f'with the reason it must not ship.')
    return problems


def declared_bundle_files() -> tuple[str, ...]:
    """ws13_seed.BUNDLE_FILES, read statically.

    Read rather than imported: ws13_seed imports boto3 and psycopg at module
    scope, and this builder must run on a checkout that has neither. Reading
    the literal also keeps the two lists honest in the direction that matters
    -- ws13_seed.bundle_files() aborts the seeding node by name when one of
    these is absent, so anything it names is, by that program's own
    definition, required in the bundle.
    """
    tree = ast.parse(source_path('pipelines/ws13_seed.py').read_text('utf-8'),
                     filename='ws13_seed.py')
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
        if 'BUNDLE_FILES' in targets:
            return tuple(ast.literal_eval(node.value))
    sys.exit('pipelines/ws13_seed.py no longer defines BUNDLE_FILES; this '
             'builder verifies the bundle against that list and cannot '
             'silently stop doing so')


def seed_problems(members: list[tuple[str, str, bytes]]) -> list[str]:
    shipped = {name for name, _relative, _data in members}
    return [f'ws13_seed.BUNDLE_FILES names {name}, which is not in MEMBERS: '
            f'the seeding node aborts on it by name'
            for name in declared_bundle_files() if name not in shipped]


# What the launch template runs from /opt/ws13. Matched rather than parsed as
# YAML: the UserData is a !Sub block of shell, so a YAML load gives back one
# string and the invocations have to be found in it either way.
INVOCATION_RE = re.compile(r'python3 (ws13_[a-z0-9_]+\.py)')
FLEET_TEMPLATE = 'infra/ws13_fleet.yaml'


def template_invocations() -> set:
    """Every `python3 ws13_*.py` the fleet runs, from wherever it lives now.

    The template and the bundled shell, both. The invocations moved out of
    the UserData with the rest of the scripts, so a check that read only the
    template would now be reading a file that invokes nothing -- and passing.
    """
    sources = [source_path(FLEET_TEMPLATE)]
    sources += [source_path(relative) for name, relative, _why in MEMBERS
                if name.endswith('.sh')]
    found = set()
    for source in sources:
        if source.is_file():
            found |= set(INVOCATION_RE.findall(source.read_text('utf-8')))
    return found


def template_problems(members: list[tuple[str, str, bytes]]) -> list[str]:
    """Entry points infra/ws13_fleet.yaml invokes that the archive lacks.

    The third way a hand-built bundle goes wrong, after a forgotten import
    and a forgotten .sql: a new entry point added to the UserData and not to
    MEMBERS. That node boots, reaches `python3: can't open file
    '/opt/ws13/...'`, and in confidence mode does it while holding a claimed
    shard slot -- so the slot reads as running and its pages are measured by
    nobody.

    A missing template is a hard failure, not a skip. This check silently
    passing because the file moved is the same class of defect it exists to
    catch.
    """
    path = source_path(FLEET_TEMPLATE)
    if not path.is_file():
        return [f'{FLEET_TEMPLATE}: not found, so the launch template\'s '
                f'entry points could not be checked against MEMBERS']
    invoked = template_invocations()
    if not invoked:
        return [f'no `python3 ws13_*.py` invocation found in '
                f'{FLEET_TEMPLATE} or in any bundled *.sh, which means this '
                f'check is no longer reading what the nodes run']
    shipped = {name for name, _relative, _data in members}
    return [f'the fleet runs `python3 {name}` from /opt/ws13, and it '
            f'is not in MEMBERS: that node boots into '
            f'"can\'t open file \'/opt/ws13/{name}\'"'
            for name in sorted(invoked - shipped)]


def build_manifest(members: list[tuple[str, str, bytes]]) -> bytes:
    """The record that travels with the code, as deterministic JSON."""
    document = {
        'bundle': KEY,
        'members': [
            {'name': name,
             'source': relative,
             'sha256': hashlib.sha256(data).hexdigest(),
             'bytes': len(data),
             'why': why}
            for (name, relative, data), (_n, _r, why)
            in zip(members, MEMBERS)
        ],
    }
    return (json.dumps(document, indent=2, sort_keys=False) + '\n').encode()


def tar_bytes(members: list[tuple[str, str, bytes]],
              manifest: bytes) -> bytes:
    """A byte-reproducible .tar.gz of the members plus the manifest.

    Every field a tar records about the build host is pinned or cleared:
    mtime, uid, gid, uname, gname and mode. GzipFile is given mtime=0 as well,
    because gzip writes the compression time into its own header and that
    alone would make two builds of identical inputs differ.
    """
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode='w', format=tarfile.PAX_FORMAT) as tar:
        entries = [(name, data) for name, _relative, data in members]
        entries.append((MANIFEST_NAME, manifest))
        for name, data in sorted(entries):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mtime = EPOCH
            info.mode = (EXEC_MODE if name.endswith(EXECUTABLE_SUFFIX)
                         else FILE_MODE)
            info.uid = info.gid = 0
            info.uname = info.gname = ''
            info.type = tarfile.REGTYPE
            tar.addfile(info, io.BytesIO(data))
    packed = io.BytesIO()
    with gzip.GzipFile(fileobj=packed, mode='wb', compresslevel=9,
                       mtime=EPOCH) as zipped:
        zipped.write(raw.getvalue())
    return packed.getvalue()


def build() -> tuple[bytes, list[tuple[str, str, bytes]], bytes]:
    """(archive bytes, members, manifest bytes), or exit with the reason."""
    assert_no_collisions()
    members = read_members()
    problems = (closure_problems(members) + seed_problems(members)
                + template_problems(members))
    if problems:
        sys.exit('the bundle does not carry everything a node will ask it '
                 'for:\n  ' + '\n  '.join(problems))
    manifest = build_manifest(members)
    return tar_bytes(members, manifest), members, manifest


def verify(path: Path) -> int:
    """Compare an existing archive against these sources. -> exit status.

    The question this answers is the one a fixed S3 key cannot: is the object
    an operator uploaded the bundle that these sources build? Both halves are
    reported, because they fail differently -- a member whose bytes differ is
    a stale or edited file, while a member that is present in one and not the
    other is a MEMBERS list that moved.
    """
    if not path.is_file():
        print(f'{path}: no such archive', file=sys.stderr)
        return 2
    try:
        with tarfile.open(path, 'r:gz') as tar:
            found, junk = {}, []
            for info in tar.getmembers():
                if not info.isfile():
                    continue
                # `tar czf` from a directory writes './name'; this builder
                # writes 'name'. They extract to the same path, so comparing
                # the spellings would report every member of a hand-built
                # archive as both missing and unexpected -- 42 lines of noise
                # over the one fact that matters, which is which files are
                # actually in there.
                name = info.name[2:] if info.name.startswith('./') else info.name
                # AppleDouble resource forks. macOS tar emits one per file
                # unless COPYFILE_DISABLE=1 is set, and they extract onto the
                # node as dot-files beside the code. They are not members and
                # never were; they are the fingerprint of a hand-build on a
                # Mac, so they are named as that rather than as a mystery.
                if os.path.basename(name).startswith('._'):
                    junk.append(name)
                    continue
                handle = tar.extractfile(info)
                found[name] = handle.read() if handle else b''
    except (tarfile.TarError, OSError) as exc:
        print(f'{path}: not a readable tar.gz ({exc})', file=sys.stderr)
        return 2

    expected = {name: data for name, _relative, data in read_members()}
    expected[MANIFEST_NAME] = build_manifest(read_members())
    problems = []
    for name in sorted(set(expected) - set(found)):
        problems.append(f'{name}: missing from the archive')
    for name in sorted(set(found) - set(expected)):
        problems.append(f'{name}: in the archive but not in MEMBERS')
    if junk:
        problems.append(
            f'{len(junk)} AppleDouble resource fork(s) ({", ".join(junk[:3])}'
            f'{" ..." if len(junk) > 3 else ""}): this archive was built by '
            f'hand with macOS tar and no COPYFILE_DISABLE=1')
    for name in sorted(set(found) & set(expected)):
        if found[name] != expected[name]:
            problems.append(
                f'{name}: differs -- archive '
                f'{hashlib.sha256(found[name]).hexdigest()[:16]}, sources '
                f'{hashlib.sha256(expected[name]).hexdigest()[:16]}')
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    print(f'{path}')
    print(f'  sha256 {digest}')
    print(f'  {len(found)} member(s)')
    if problems:
        print('  DOES NOT MATCH these sources:')
        for problem in problems:
            print(f'    {problem}')
        return 1
    print('  matches these sources exactly')
    return 0


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--output', default=str(DEFAULT_OUTPUT),
                        help=f'where to write the archive '
                             f'(default {DEFAULT_OUTPUT})')
    parser.add_argument('--upload', action='store_true',
                        help='print the aws s3 cp line to run. This script '
                             'never uploads: publishing is a separate '
                             'decision from building, taken after reading '
                             'the digest')
    parser.add_argument('--verify', metavar='ARCHIVE',
                        help='compare an existing archive against these '
                             'sources and exit non-zero on any difference')
    parser.add_argument('--list', action='store_true',
                        help='print the member list and why each is there')
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    if args.verify:
        return verify(Path(args.verify))
    if args.list:
        for name, relative, why in MEMBERS:
            print(f'{name:32} {relative:44} {why}')
        return 0

    archive, members, manifest = build()
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(archive)
    digest = hashlib.sha256(archive).hexdigest()

    print(f'wrote {output}')
    print(f'  {len(members) + 1} members, {len(archive):,} bytes')
    print(f'  sha256 {digest}')
    print(f'  verified: closed under its own ws13_* imports, carries every '
          f'name in')
    print(f'            ws13_seed.BUNDLE_FILES, and carries every '
          f'`python3 ws13_*.py` the')
    print(f'            template and the bundled *.sh invoke')
    print(f'  {MANIFEST_NAME} carries a sha256 per member and untars to '
          f'/opt/ws13/{MANIFEST_NAME}')
    del manifest

    print()
    print('This bundle is what a node runs. The ASG is at 0, so uploading it '
          'changes nothing')
    print('until the next scale-up -- and until it is uploaded, that '
          'scale-up runs the OLD code.')
    print()
    print('The upload is a human step. Run:')
    print(f'  aws s3 cp {output} s3://{BUCKET}/{KEY}')
    print('then confirm what landed:')
    print(f'  aws s3api head-object --bucket {BUCKET} --key {KEY} '
          f'--query "[ContentLength,LastModified]" --output text')
    print(f'and on any node: cat /opt/ws13/{MANIFEST_NAME}')
    if args.upload:
        print()
        print('--upload prints the command and stops. This script has no S3 '
              'write path at all -- not')
        print('because the bucket is unwritable (it is not), but because '
              'building and publishing are')
        print('different decisions. --verify and the digest above sit '
              'between them, and a builder that')
        print('uploaded on success would run both only after the fleet '
              'could already download the result.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
