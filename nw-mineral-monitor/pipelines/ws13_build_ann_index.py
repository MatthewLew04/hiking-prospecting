#!/usr/bin/env python3
"""Phase A: build the HNSW index the retrieval path needs, on the VPC host.

ws13_chunks holds 852,027 rows whose titan_embedding vector(1024) is complete
and unit-norm, and there is no ANN index on any vector column. Every vector
query is therefore a sequential scan over all 852,027 rows, which is well
past the 30 s API Gateway deadline in front of the retrieval Lambda long
before it is anywhere near the Lambda's own 60 s timeout.

This runs ON the in-VPC host (i-0818521a8b3ff7c90, t4g.small), not on a
laptop: nwmm-ws13 has no public endpoint and policy blocks pulling the DB
secret to a local machine. tools/ws13_ssm.py is the local half that ships a
script here. Ship infra/ws13_query_lambda.py and pipelines/
ws13_index_contract.py alongside this file: the halfvec expression, the index
name and the probe SQL are imported from them, never re-typed here. The fleet
untars the bundle FLAT into /opt/ws13, so the query Lambda is resolved from
beside this file as well as from the repository layout -- a repo-only default
sent every on-host step looking for /opt/infra/ws13_query_lambda.py and
aborted before it did anything.

Five steps, each selected on its own and each rerun-safe:

  --pause-backfill  stop ws13_embed_backfill.py without stopping the instance
  --build           SET the build memory, CREATE INDEX, then ANALYZE
  --verify          EXPLAIN the shapes production issues; require the index
  --measure         p95 latency at hnsw.ef_search 40 / 100 / 200
  --resume-backfill print (never run) the command that restarts the backfill

--dry-run is the default and governs the two steps that change the system,
--pause-backfill and --build; pass --yes to actually act. --verify,
--measure and --resume-backfill are read-only or print-only and always run,
which is what lets deploy.sh preflight call --verify as a gate.

A GATE MUST NOT BE THE OUTAGE. --verify checks pg_class for the index before
it plans anything, bounds the session with a statement_timeout, and issues a
PLAIN EXPLAIN: EXPLAIN (ANALYZE) executes the statement, so a gate built on
it would answer "is the index used?" by performing the 852,027-row sequential
scan whose absence it is checking for -- against production RDS, in deploy
preflight, unbounded. EXPLAIN (ANALYZE, BUFFERS) appears exactly once, in the
operator's deliberate --measure run, after the index is known to exist.

--verify certifies the FILTERED shapes too. Real requests carry filters,
filter_sql() flattens into a semi-join, and the semi-join is what can push
the planner off the HNSW ordered path; certifying only the bare probe
certifies a statement no request ever issues. The statements come from the
Lambda's own plan_filtered_probe() and explain_ann_sql(), so what is planned
here is what production runs.

Exit codes, because deploy.sh preflight keys on them:

  0  the selected steps passed
  1  usage: no step selected, no DSN, a bad argument
  2  the offline contract is broken (or the query Lambda cannot be read)
  3  --pause-backfill refused: an unrecognised parent
  4  --build could not build a valid index
  5  the plan assertion failed, or the index is absent/INVALID
  6  the database could not be opened at all, so the gate was NOT evaluated

The JSON report goes to stdout as well as to --report, because over SSM
stdout is the only thing that comes back.
"""

from __future__ import annotations

import argparse
import datetime as dt
import errno
import glob
import json
import math
import os
from pathlib import Path
import re
import signal
import socket
import sys
import time
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ws13_index_contract as contract                             # noqa: E402


DEFAULT_QUERY_LAMBDA = contract.QUERY_LAMBDA
BACKFILL_MARKER = 'ws13_embed_backfill'
BACKFILL_SCRIPT_RE = re.compile(r'(^|[\s/])ws13_embed_backfill\.py(\s|$)')
SELF_MARKER = 'ws13_build_ann_index'
# A process that merely names the backfill is not the backfill.
INSPECTORS = ('grep', 'egrep', 'fgrep', 'pgrep', 'ps', 'awk', 'sed', 'tail',
              'less', 'vi', 'vim', 'nano', 'cat')
# Cloud-init user data on these nodes ends with a shutdown. Killing the
# python lets the shell walk to that line, and an instance-initiated
# shutdown is a STOP: user data does not re-run on start, and this node is in
# no ASG, so nothing brings it back.
CLOUD_INIT_MARKERS = ('/var/lib/cloud/', 'cloud-init', 'user-data', 'part-001')
SHUTDOWN_MARKER = 'shutdown -h now'
# db.m7g.large is 2 vCPU / 8 GiB. maintenance_work_mem holds the HNSW graph
# while it is built; spilling turns hours into a day. 3 GB fits alongside
# shared_buffers on an 8 GiB instance, and it is enough only because the
# index is built on the halfvec cast: 2.18 GiB of build memory instead of
# 3.81 GiB, and a 2.5 GB index instead of 7.2 GB, at a measured recall cost
# of zero (recall@10 was 100% over 6 probes, max distance delta 1.79e-05).
BUILD_SETTINGS = (
    ('maintenance_work_mem', '3GB'),
    ('max_parallel_maintenance_workers', '2'),
    # An inherited statement_timeout would abort the build and leave an
    # INVALID index behind, which is the one state this script refuses to
    # clean up on its own. Measured 2026-08-27: 626.3 s to build plus 49.3 s
    # to ANALYZE, over 848,032 rows on db.m7g.large (2 vCPU / 8 GB) at these
    # settings. That is well inside any timeout anyone would set -- but the
    # reason to pin it to 0 was never the duration, it was that a build cut
    # off partway leaves an index nothing can use and this script will not
    # drop.
    ('statement_timeout', '0'),
)
EF_LADDER = (40, 100, 200)
DEFAULT_EF = 200
DEFAULT_LIMIT = 200
DEFAULT_PROBES = 20
# The real budget. The Lambda may sit for 60 s; the API Gateway integration in
# front of it gives up at 30 s, so 30 s is the number every measurement below
# is read against.
API_GATEWAY_DEADLINE_S = 30
LAMBDA_TIMEOUT_S = 60
# Bounds every read-only step. A gate that hangs is a worse outage than the
# one it was watching for, and 60 s is already twice the API Gateway deadline
# every measurement below is read against: a statement that needs longer has
# failed whatever the plan says. --build sets its own statement_timeout = 0,
# because a build that is cut off partway leaves an INVALID index behind.
STATEMENT_TIMEOUT_MS = 60000
# The EXISTS side of a filtered probe reads ws13_documents (56,282 rows).
# That scan is not the failure this gate is about; a scan of the 852,027-row
# chunk table is, whatever else the plan does.
FILTERED_SEQ_SCAN_OK = ('ws13_documents',)
# The secret NAME, not the ARN. 'ws13/postgres-7wq3XL' -- the form the
# original handoff prescribed and both of these copied -- appends the ARN's
# random suffix to the name and is AccessDenied against the fleet role,
# whose policy grants GetSecretValue on the secret's own ARN. It never
# worked; it was simply never reached, because the fallback only runs once
# the backfill process is gone and there is no DSN left to borrow. That
# process has now exited, so this path is the only one there is.
SECRET_ID = 'ws13/postgres'
DB_NAME = 'nwmm'
DB_PORT = 5432
EXIT_OK = 0
EXIT_CONTRACT = 2
EXIT_PAUSE_REFUSED = 3
EXIT_BUILD = 4
EXIT_PLAN = 5
# deploy.sh preflight degrades to a clear skip on this one and only this one:
# "the database is unreachable from here" is not the same answer as "the
# index is missing", and only the second is a reason to stop a deploy.
EXIT_UNREACHABLE = 6


def log(msg):
    print(f'{dt.datetime.now(dt.timezone.utc).isoformat()} {msg}', flush=True)


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--pause-backfill', action='store_true',
                   help='stop ws13_embed_backfill.py (and its user-data parent)')
    p.add_argument('--build', action='store_true',
                   help='CREATE INDEX then ANALYZE')
    p.add_argument('--verify', action='store_true',
                   help='plain-EXPLAIN the unfiltered AND filtered probes and '
                        'require the index in every plan that needs it')
    p.add_argument('--measure', action='store_true',
                   help='p95 probe latency across the ef_search ladder')
    p.add_argument('--resume-backfill', action='store_true',
                   help='print the restart command; never runs it')
    p.add_argument('--dsn', default=os.environ.get('WS13_DB_DSN'),
                   help='libpq DSN (default WS13_DB_DSN)')
    p.add_argument('--query-lambda', default=str(DEFAULT_QUERY_LAMBDA),
                   help='module exporting the halfvec contract constants')
    p.add_argument('--probes', type=int, default=DEFAULT_PROBES,
                   help='probe vectors drawn from existing rows')
    p.add_argument('--limit', type=int, default=DEFAULT_LIMIT,
                   help='probe LIMIT (the over-fetch a real request uses)')
    p.add_argument('--ef-search', type=int, action='append', default=[],
                   help='ef_search value to measure (repeatable)')
    p.add_argument('--pause-timeout', type=int, default=60,
                   help='seconds to wait for a signalled process to exit')
    p.add_argument('--report', help='write the JSON report here as well')
    p.add_argument('--dry-run', action='store_true',
                   help='the default: describe the mutating steps, do nothing')
    p.add_argument('--yes', action='store_true',
                   help='actually pause the backfill and build the index')
    return p.parse_args(argv)


# --- step a: pause the backfill without stopping the instance --------------

def read_proc(pid, name):
    with open(f'/proc/{pid}/{name}', 'rb') as fh:
        return fh.read()


def proc_argv(pid):
    raw = read_proc(pid, 'cmdline').decode('utf-8', 'replace')
    return ' '.join(token for token in raw.split('\0') if token)


def proc_status(pid):
    fields = {}
    for line in read_proc(pid, 'status').decode('utf-8', 'replace').splitlines():
        key, _, value = line.partition(':')
        fields[key] = value.strip()
    return fields


def proc_alive(pid):
    """A zombie is gone for our purposes: it runs no more shell lines."""
    try:
        return proc_status(pid).get('State', '').split()[0] != 'Z'
    except (OSError, IndexError):
        return False


def find_backfill():
    """Every live ws13_embed_backfill.py, with its parent.

    The only half of this step that touches /proc or the filesystem.
    Everything that DECIDES anything is plan_pause() over the dicts this
    returns, so the decision is testable without a host to kill things on.
    """
    found = []
    for path in sorted(glob.glob('/proc/[0-9]*/cmdline')):
        pid = int(path.split('/')[2])
        if pid == os.getpid():
            continue
        try:
            argv = proc_argv(pid)
        except OSError:
            continue                      # exited between the glob and the read
        if BACKFILL_MARKER not in argv or SELF_MARKER in argv:
            continue
        if not BACKFILL_SCRIPT_RE.search(argv):
            continue
        if argv.split()[0].rsplit('/', 1)[-1] in INSPECTORS:
            continue
        entry = {'pid': pid, 'argv': argv, 'ppid': None, 'parent_argv': None,
                 'parent_name': None}
        try:
            # ppid stays None unless this line succeeded: classify_parent()
            # treats "not read" as unknown, and unknown is refused.
            entry['ppid'] = int(proc_status(pid)['PPid'])
            entry['parent_argv'] = proc_argv(entry['ppid'])
            entry['parent_name'] = proc_status(entry['ppid']).get('Name')
        except (OSError, KeyError, ValueError):
            pass
        script, has_shutdown = user_data_script(entry['parent_argv'])
        entry['user_data_script'] = script
        entry['user_data_ends_in_shutdown'] = has_shutdown
        found.append(entry)
    return found


def classify_parent(entry):
    """('orphaned' | 'cloud_init' | 'unrecognised', why).

    Only two parentages are recognised. Anything else is refused rather than
    guessed at, because the cost of guessing wrong is an instance-initiated
    STOP on a node nothing will restart.

    Pure: it reads the dict find_backfill() built and never /proc, so the one
    decision on this host that can stop an instance is testable.
    """
    # ppid=None means the PPid line could not be READ -- a status read racing
    # an exec, a /proc mounted hidepid, a permission this process does not
    # have. Reporting that as 'orphaned' was a fail-OPEN branch in the one
    # function that promises to refuse what it cannot identify: pause_backfill
    # would then signal only the child, and a live user-data shell whose
    # foreground child just died walks on to `shutdown -h now`. Unknown is
    # unrecognised, and unrecognised is refused.
    if entry.get('ppid') is None:
        return 'unrecognised', ('the parent PID of this process could not be '
                                'read, so nothing here knows whether killing '
                                'the child lets a shell walk on to a shutdown')
    if entry['ppid'] in (0, 1):
        return 'orphaned', (f'parent is PID {entry["ppid"]}: the process is '
                            'already orphaned, so no shell can walk on to a '
                            'shutdown line')
    parent_argv = entry['parent_argv']
    if not parent_argv:
        return 'unrecognised', (f'parent PID {entry["ppid"]} '
                                f'({entry["parent_name"]}) has no readable '
                                'command line')
    if any(marker in parent_argv for marker in CLOUD_INIT_MARKERS):
        return 'cloud_init', (f'parent PID {entry["ppid"]} is a cloud-init / '
                              f'user-data shell: {parent_argv}')
    return 'unrecognised', (f'parent PID {entry["ppid"]} is not a shape this '
                            f'script knows: {parent_argv}')


def user_data_script(parent_argv):
    """The user-data script path, and whether it really ends in a shutdown."""
    for token in (parent_argv or '').split():
        if token.startswith('/var/lib/cloud/') and os.path.isfile(token):
            try:
                text = Path(token).read_text(errors='replace')
            except OSError:
                return token, None
            return token, SHUTDOWN_MARKER in text
    return None, None


def manual_commands(entry):
    """What to hand a human when the parentage is not one we recognise.

    Deliberately not phrased as an instruction: an unrecognised parent might
    be a tmux session, where killing the parent is wrong, or a shell that
    walks on to `shutdown -h now`, where killing only the child is wrong.
    Judging which is the human's job, so both orders are laid out and neither
    is run.
    """
    return [
        f'# refusing to act: parent PID {entry["ppid"]} '
        f'({entry["parent_name"]}) is not a recognised shape.',
        f'ps -o pid,ppid,lstart,cmd -p {entry["pid"]},{entry["ppid"]}',
        '# if that parent is a shell that continues to `shutdown -h now`, it',
        '# must die FIRST -- an instance-initiated shutdown is a STOP, user',
        '# data does not re-run on start, and this node is in no ASG:',
        f'kill -TERM {entry["ppid"]}',
        '# then the backfill itself (alone, if the parent is a session you',
        '# want to keep):',
        f'kill -TERM {entry["pid"]}',
    ]


def looks_like_target(argv):
    """True when a re-read command line still looks like something to signal.

    Pure, and separate from the kill loop on purpose: PIDs are recycled, and
    killing a recycled PID on a live host is exactly the accident this step
    exists to avoid.
    """
    return (BACKFILL_MARKER in argv
            or any(marker in argv for marker in CLOUD_INIT_MARKERS))


def plan_pause(found):
    """What to signal, in what order -- decided over dicts, not over /proc.

    Pure: no /proc, no filesystem, no os.kill. This is the half that decides
    whether a live user-data shell gets a SIGTERM, so it is the half that has
    to be testable with synthetic entries; find_backfill() is the half that
    reads the host.

    result['targets'] is [[pid, label], ...] in signalling order, and it is
    empty whenever ok is False.
    """
    result = {'step': 'pause-backfill', 'executed': False, 'processes': [],
              'commands': [], 'targets': [], 'ok': True, 'note': None}
    if not found:
        result['note'] = ('no ws13_embed_backfill.py running; already paused '
                          '(this step is rerun-safe)')
        return result

    targets = []
    for entry in found:
        entry = dict(entry)
        kind, why = classify_parent(entry)
        entry.update({'parent_kind': kind, 'reason': why})
        result['processes'].append(entry)
        if kind == 'unrecognised':
            result['ok'] = False
            result['commands'].extend(manual_commands(entry))
            continue
        # bash does not act on a deferred SIGTERM until its foreground child
        # exits, so the parent is signalled first and only reaped once the
        # python goes. Signalling the child first is what lets the shell reach
        # its next line.
        if kind == 'cloud_init':
            targets.append([entry['ppid'], 'user-data shell'])
        targets.append([entry['pid'], 'ws13_embed_backfill.py'])

    if not result['ok']:
        # One unrecognised parent stops the whole step, including the entries
        # that WERE recognised: the operator is about to read these commands,
        # and a half-executed pause is the state nobody can reason about.
        result['note'] = ('refusing to kill anything: at least one parent is '
                          'unrecognised. Run the printed commands by hand '
                          'after checking them.')
        return result

    result['targets'] = targets
    result['commands'] = [f'kill -TERM {pid}   # {label}'
                          for pid, label in targets]
    # The paused process knows its own argv, which is what --resume-backfill
    # should hand back. Its environ also holds WS13_DB_DSN, which is what
    # tools/ws13_ssm.py --with-dsn borrows -- after this step that source is
    # gone and the Secrets Manager fallback is the only one left.
    result['observed_argv'] = [entry['argv'] for entry in found]
    return result


def pause_backfill(execute, timeout):
    """Stop the backfill, and its user-data shell first when there is one."""
    if not os.path.isdir('/proc'):
        return {'step': 'pause-backfill', 'executed': False, 'processes': [],
                'commands': [], 'targets': [], 'ok': False,
                'note': ('no /proc: this step only runs on the in-VPC Linux '
                         'host, not on an operator laptop')}

    result = plan_pause(find_backfill())
    targets = [tuple(target) for target in result['targets']]
    if not result['ok'] or not targets:
        return result
    if not execute:
        result['note'] = 'dry run: nothing signalled'
        return result

    for pid, label in targets:
        # Re-read the command line immediately before signalling: PIDs are
        # recycled, and killing a recycled PID on a live host is exactly the
        # accident this whole step exists to avoid.
        try:
            argv = proc_argv(pid)
        except OSError:
            log(f'pid {pid} ({label}) already gone')
            continue
        if not looks_like_target(argv):
            result['ok'] = False
            result['note'] = (f'pid {pid} no longer looks like {label} '
                              f'({argv}); refusing to signal it')
            return result
        os.kill(pid, signal.SIGTERM)
        log(f'SIGTERM -> {pid} ({label})')
    result['executed'] = True

    deadline = time.time() + timeout
    survivors = [pid for pid, _ in targets]
    while time.time() < deadline and survivors:
        survivors = [pid for pid in survivors if proc_alive(pid)]
        if survivors:
            time.sleep(1)
    # A surviving parent is precisely the process that would run
    # `shutdown -h now`, so leaving it alive is the unsafe outcome, not the
    # cautious one. The backfill itself is resumable by construction (it scans
    # for NULL columns), so a hard kill costs nothing but the batch in flight.
    for pid in survivors:
        try:
            os.kill(pid, signal.SIGKILL)
            log(f'SIGKILL -> {pid} (still alive after {timeout}s)')
        except OSError as exc:
            if exc.errno != errno.ESRCH:
                raise
    time.sleep(2)
    still = [pid for pid in (pid for pid, _ in targets) if proc_alive(pid)]
    if still:
        result['ok'] = False
        result['note'] = f'still running after SIGKILL: {still}'
    else:
        result['note'] = 'backfill paused'
    return result


# --- database helpers ------------------------------------------------------

class DatabaseUnreachable(RuntimeError):
    """The database could not be opened at all, so no gate was evaluated.

    Distinct from every other failure on purpose: "I could not reach the
    database from here" is not the same answer as "the index is missing", and
    deploy.sh preflight is entitled to skip on the first and must stop on the
    second. main() turns this into exit 6.
    """


def scrub(text):
    """Anything DSN-shaped, redacted.

    libpq errors name the host, not the password, but this whole workstream
    depends on the credential never reaching a log, so it is scrubbed before
    it is printed rather than after someone notices.
    """
    return re.sub(r'://[^\s@]*@', '://[redacted]@', str(text))


def connect(dsn):
    """Open the DB. psycopg is imported here, not at module scope, so the
    print-only steps still work where it is not installed."""
    if not dsn:
        sys.exit('need --dsn or WS13_DB_DSN for this step')
    try:
        import psycopg                                            # noqa: PLC0415
    except ImportError:
        raise DatabaseUnreachable(
            'psycopg (v3) is not installed here, so this host cannot open the '
            'WS13 database; it is installed on the in-VPC host by the '
            'launch-template user data') from None
    try:
        return psycopg.connect(dsn, autocommit=True)
    except Exception as exc:
        raise DatabaseUnreachable('cannot connect to the WS13 database: '
                                  + scrub(exc)) from None


def set_config(conn, name, value):
    """SET, in the form that takes a bound parameter.

    `SET x = y` accepts only a literal, so the parameterised equivalent is
    set_config(name, value, false) -- same session scope, no interpolation.
    """
    return conn.execute('SELECT set_config(%s, %s, false)',
                        (name, str(value))).fetchone()[0]


def index_state(conn, name):
    """(exists, usable, bytes) for the ANN index.

    indisready as well as indisvalid: an interrupted or failed CREATE INDEX
    leaves an index that exists in pg_class, is never used by the planner, and
    would put every probe straight back into the sequential scan over 852,027
    rows. Two catalogue rows, no scan, no timeout -- which is why every step
    that would otherwise plan or run a probe asks this FIRST.
    """
    row = conn.execute(
        'SELECT i.indisvalid AND i.indisready, pg_relation_size(c.oid) '
        '  FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid '
        ' WHERE c.relkind = %s AND c.relname = %s', ('i', name)).fetchone()
    if row is None:
        return False, False, 0
    return True, bool(row[0]), int(row[1])


def require_usable_index(conn, consts, result):
    """Fill in the index state; return the refusal message, or None.

    THE line this module exists for. Without this check the answer to "is the
    index used?" is discovered by planning -- or, with EXPLAIN (ANALYZE),
    RUNNING -- a probe against 852,027 rows with no index to serve it, in a
    deploy preflight, against production RDS.
    """
    exists, usable, size = index_state(conn, consts['INDEX_NAME'])
    result.update({'index_exists': exists, 'index_usable': usable,
                   'bytes': size})
    if exists and usable:
        return None
    if not exists:
        return (f'{consts["INDEX_NAME"]} does not exist: refusing to probe '
                'ws13_chunks without it, because that is a sequential scan '
                'over 852,027 rows. Build it with --build first.')
    return (f'{consts["INDEX_NAME"]} exists but is INVALID or not ready (the '
            'leftover of an interrupted CREATE INDEX): the planner never uses '
            'it, so a probe would be the same sequential scan. Drop it '
            f'deliberately and rerun --build: DROP INDEX {consts["INDEX_NAME"]};')


def probe_params(sql, vector_text, limit):
    """Bind the probe vector, and a limit when EXPLAIN_SQL asks for one."""
    holes = sql.count('%s')
    if holes == 1:
        return (vector_text,)
    if holes == 2:
        return (vector_text, limit)
    raise contract.ContractError(
        f'EXPLAIN_SQL binds {holes} positional parameters; this script knows '
        'how to supply the probe vector and at most a limit')


def probe_vectors(conn, count):
    """Probe vectors drawn from existing rows, spread evenly over the id range.

    Deterministic on purpose, so two runs compare like with like, and cheap:
    ORDER BY random() over 852,027 rows costs a full scan per probe, while
    each of these is one index-driven lookup on the primary key.

    The vector comes back as its text form and goes straight back as a bound
    parameter for `%s::halfvec(1024)`; pgvector's Python adapter is not one of
    this project's dependencies and is not needed for that round trip.
    """
    bounds = conn.execute(
        'SELECT min(id), max(id) FROM ws13_chunks').fetchone()
    if not bounds or bounds[0] is None:
        raise contract.ContractError('ws13_chunks is empty')
    low, high = int(bounds[0]), int(bounds[1])
    step = max(1, (high - low) // max(1, count))
    vectors = []
    for i in range(count):
        row = conn.execute(
            'SELECT titan_embedding::text FROM ws13_chunks '
            ' WHERE id >= %s AND titan_embedding IS NOT NULL '
            ' ORDER BY id LIMIT 1', (low + i * step,)).fetchone()
        if row:
            vectors.append(row[0])
    if not vectors:
        raise contract.ContractError('no embedded rows to probe with')
    return vectors


def probe_query(consts):
    """EXPLAIN_SQL minus its EXPLAIN wrapper: the bare unfiltered statement.

    Two callers. --measure times it directly, because EXPLAIN (ANALYZE)
    instruments every plan node and inflates exactly the number that step
    exists to compare against a 30 s budget. --verify puts a PLAIN EXPLAIN
    back in front of it, so the gate costs the plan whatever options the
    constant carries. Either way the result is checked to still contain
    ORDER_BY_SQL byte for byte, so the statement that runs is the statement
    the plan assertion proved.
    """
    sql = re.sub(r'^\s*EXPLAIN\s*(\([^)]*\))?\s*', '', consts['EXPLAIN_SQL'],
                 count=1, flags=re.I)
    if not sql.lstrip().upper().startswith('SELECT'):
        raise contract.ContractError('EXPLAIN_SQL does not wrap a SELECT')
    if consts['ORDER_BY_SQL'] not in sql:
        raise contract.ContractError('stripping EXPLAIN lost the ORDER BY')
    return sql


# --- step b: build ---------------------------------------------------------

def build_index(conn, consts, execute):
    result = {'step': 'build', 'executed': False, 'ok': True,
              'index': consts['INDEX_NAME'], 'sql': consts['CREATE_INDEX_SQL'],
              'settings': dict(BUILD_SETTINGS)}
    exists, valid, size = index_state(conn, consts['INDEX_NAME'])
    result.update({'existed': exists, 'valid': valid, 'bytes': size})
    if exists and valid:
        result['note'] = (f'{consts["INDEX_NAME"]} already exists and is valid '
                          f'({size / 2 ** 30:.2f} GiB); nothing to do')
        return result
    if exists and not valid:
        # The leftover of an interrupted CREATE INDEX. It is never used by the
        # planner and it is never repaired by rebuilding around it, but
        # dropping an object is the operator's call, not this script's.
        result['ok'] = False
        result['note'] = (f'{consts["INDEX_NAME"]} exists but is INVALID; drop '
                          f'it deliberately and rerun: '
                          f'DROP INDEX {consts["INDEX_NAME"]};')
        return result

    rows = conn.execute(
        "SELECT reltuples::bigint FROM pg_class WHERE relname = 'ws13_chunks'"
    ).fetchone()
    result['estimated_rows'] = int(rows[0]) if rows and rows[0] else None
    if not execute:
        result['note'] = 'dry run: nothing built'
        return result

    for name, value in BUILD_SETTINGS:
        applied = set_config(conn, name, value)
        log(f'set {name} = {applied}')
    # No duration promised here. This said "this runs for hours" until the
    # first real run took 626.3 s -- a hardcoded guess, wrong by ~20x, printed
    # to an operator deciding whether to keep a terminal open. The measured
    # figure lives beside BUILD_SETTINGS with the hardware it was measured on;
    # a line printed before the work starts is the wrong place to assert how
    # long the work takes.
    log(f'creating {consts["INDEX_NAME"]} over '
        f'{result["estimated_rows"]} estimated rows')
    started = time.perf_counter()
    conn.execute(consts['CREATE_INDEX_SQL'])
    result['create_seconds'] = round(time.perf_counter() - started, 1)
    log(f'index built in {result["create_seconds"]}s; analyzing')
    started = time.perf_counter()
    conn.execute(consts['ANALYZE_SQL'])
    result['analyze_seconds'] = round(time.perf_counter() - started, 1)
    _, valid, size = index_state(conn, consts['INDEX_NAME'])
    result.update({'executed': True, 'valid': valid, 'bytes': size,
                   'note': f'built {size / 2 ** 30:.2f} GiB, valid={valid}'})
    result['ok'] = valid
    return result


# --- step c: assert the plan ----------------------------------------------

def verify_filters(conn):
    """The filter sets to certify, drawn from the corpus itself.

    Hardcoded values would certify a shape only if this database happened to
    hold documents matching them: a filter that matches nothing resolves to
    the 'no_documents' strategy and is never planned at all, which is how a
    filtered gate quietly degrades back into the unfiltered one it replaced.
    Each lookup is one small aggregate over the 56,282-row document table,
    under the statement timeout the caller has already set.

    Which value each lookup picks is deliberate. The BROADEST state is the
    filter most likely to exceed FILTER_SHA_CAP and stay a semi-join, which is
    the shape that can lose the HNSW index. The RAREST state+county is the
    filter most likely to resolve to a bounded sha256 set, which is the other
    statement production issues -- a different SQL shape again, and one no
    unfiltered probe says anything about.
    """
    shapes = [('unfiltered', {})]
    lookups = (
        ('state',
         'SELECT state FROM ws13_documents WHERE state IS NOT NULL '
         ' GROUP BY state ORDER BY count(*) DESC, state LIMIT 1',
         lambda row: {'state': row[0]}),
        ('state+county',
         'SELECT state, county FROM ws13_documents '
         ' WHERE state IS NOT NULL AND county IS NOT NULL '
         ' GROUP BY state, county ORDER BY count(*), state, county LIMIT 1',
         lambda row: {'state': row[0], 'county': row[1]}),
        ('admission_class',
         'SELECT admission_class FROM ws13_documents '
         ' WHERE admission_class IS NOT NULL GROUP BY admission_class '
         ' ORDER BY count(*) DESC, admission_class LIMIT 1',
         lambda row: {'admission_class': [row[0]]}),
    )
    for name, sql, build in lookups:
        try:
            row = conn.execute(sql).fetchone()
        except Exception as exc:
            log(f'verify: no {name} filter to certify ({scrub(exc)})')
            continue
        if row and row[0] is not None:
            label = f'{name}=' + '/'.join(str(value) for value in row)
            shapes.append((label, build(row)))
    return shapes


def explain_probe(module, consts, filters, shas, vector_text, limit):
    """(plain-EXPLAIN statement, params) for one filter shape.

    Nothing here re-types SQL. The unfiltered shape is EXPLAIN_SQL with its
    wrapper stripped and a PLAIN EXPLAIN put back -- so the gate stays a
    planner call even if that constant ever regains its ANALYZE. Every other
    shape comes from the Lambda's own explain_ann_sql(), so what is asserted
    is what production runs, including the sha256-set rewrite, which is a
    different statement again.
    """
    if not filters:
        sql = probe_query(consts)
        return 'EXPLAIN ' + sql, list(probe_params(sql, vector_text, limit))
    sql, params = module.explain_ann_sql(dict(filters), vector_text, limit,
                                         shas, analyze=False)
    params = list(params)
    if sql.count('%s') != len(params):
        raise contract.ContractError(
            f'explain_ann_sql() returned {sql.count("%s")} placeholders for '
            f'{len(params)} parameters; they are bound positionally')
    return sql, params


def verify_plan(conn, consts, module, vector_text, limit):
    """EXPLAIN the shapes production issues and require the index in each.

    Three deliberate refusals live here:

      * pg_class is read BEFORE anything is planned. A gate that discovers a
        missing index by probing without one is the outage it was watching
        for.
      * the session is bounded by a statement_timeout, so a regressed plan
        fails fast instead of hanging a deploy.
      * PLAIN EXPLAIN, never EXPLAIN (ANALYZE): ANALYZE runs the statement.

    The filtered shapes are the point. A semi-join is what pushes the planner
    off the HNSW ordered path, so certifying only the bare probe certifies a
    statement no real request issues.
    """
    result = {'step': 'verify', 'ok': False, 'ef_search': DEFAULT_EF,
              'shapes': [], 'problems': []}
    refusal = require_usable_index(conn, consts, result)
    if refusal:
        result['problems'] = [refusal]
        return result
    set_config(conn, 'statement_timeout', STATEMENT_TIMEOUT_MS)
    set_config(conn, 'hnsw.ef_search', DEFAULT_EF)

    certified_filtered = 0
    for label, filters in verify_filters(conn):
        shape = {'filters': label, 'strategy': 'unfiltered', 'problems': []}
        result['shapes'].append(shape)
        try:
            if filters:
                strategy, shas = module.plan_filtered_probe(conn, filters)
            else:
                strategy, shas = 'unfiltered', None
            shape['strategy'] = strategy
            shape['sha_candidates'] = None if shas is None else len(shas)
            if shas is not None and not shas:
                # The filter resolved to no document at all. Production skips
                # the probe rather than running it to prove that, so there is
                # no statement to plan -- and this shape stays uncertified.
                shape['note'] = 'filter matches no document; no probe to plan'
                continue
            sql, params = explain_probe(module, consts, filters, shas,
                                        vector_text, limit)
            shape['sql'] = sql
            rows = conn.execute(sql, params).fetchall()
        except Exception as exc:
            shape['problems'] = [f'EXPLAIN failed: {type(exc).__name__}: '
                                 f'{scrub(exc)}']
            result['problems'].extend(f'{label}: {p}' for p in shape['problems'])
            continue
        plan_text = '\n'.join(str(row[0]) for row in rows)
        shape['plan'] = plan_text
        # What the plan must show is decided by the SHAPE OF THE PROBE, not by
        # the strategy's name: a resolved sha256 set means the statement is
        # `c.sha256 = ANY(%s)`, which ws13_chunks_sha answers exactly, so
        # requiring the HNSW index there would fail a better plan than the one
        # being demanded. The sequential scan is refused either way.
        shape['problems'] = contract.plan_problems(
            plan_text, consts['INDEX_NAME'],
            allow_seq_scan_on=FILTERED_SEQ_SCAN_OK if filters else (),
            require_index=not shas)
        result['problems'].extend(f'{label}: {p}' for p in shape['problems'])
        if filters and not shape['problems']:
            certified_filtered += 1

    if not certified_filtered:
        # Not pedantry: the unfiltered probe is the one shape that cannot
        # lose the index, so a run that certified only that one has proved
        # nothing about what the retrieval path actually issues.
        result['problems'].append(
            'no FILTERED shape was certified: every real request carries '
            'filters, and the semi-join they add is the shape that can lose '
            'the index. Check that ws13_documents is populated and readable.')
    result['ok'] = not result['problems']
    return result


# --- step d: measure -------------------------------------------------------

def percentile(samples, pct):
    """Nearest-rank percentile: with 20 probes p95 is the second slowest, an
    observation rather than an interpolation nobody measured."""
    ordered = sorted(samples)
    rank = max(1, math.ceil(pct / 100.0 * len(ordered)))
    return ordered[rank - 1]


def measure(conn, consts, module, vectors, efs, limit):
    """p95 across the ef ladder, and the one deliberate EXPLAIN (ANALYZE).

    The index check at the top is not a formality. The default ladder is 3 ef
    values x (1 warm-up + 20 probes) = 63 executions; with no usable index
    every one of them is a sequential scan over 852,027 rows plus a Top-N
    sort, so the step that exists to measure a 30 s budget would instead spend
    an hour proving the index is missing -- something two catalogue rows
    answer for free.
    """
    result = {'step': 'measure', 'ok': True, 'limit': limit, 'rows': []}
    refusal = require_usable_index(conn, consts, result)
    if refusal:
        result['ok'] = False
        result['note'] = refusal
        return result
    set_config(conn, 'statement_timeout', STATEMENT_TIMEOUT_MS)
    sql = probe_query(consts)
    rows = []
    for ef in efs:
        set_config(conn, 'hnsw.ef_search', int(ef))
        # One warm-up, excluded: the first probe after a build or an ef_search
        # change pays for cold shared_buffers and would land in the p95 as a
        # cost no steady-state request pays.
        conn.execute(sql, probe_params(sql, vectors[0], limit)).fetchall()
        samples = []
        for vector in vectors:
            started = time.perf_counter()
            conn.execute(sql, probe_params(sql, vector, limit)).fetchall()
            samples.append((time.perf_counter() - started) * 1000.0)
        rows.append({'ef_search': int(ef), 'probes': len(samples),
                     'p50_ms': round(percentile(samples, 50), 1),
                     'p95_ms': round(percentile(samples, 95), 1),
                     'max_ms': round(max(samples), 1)})
    result['rows'] = rows
    # The ONE place EXPLAIN (ANALYZE, BUFFERS) belongs: a deliberate operator
    # run, after the index has been confirmed usable and under the statement
    # timeout set above. It EXECUTES the statement -- which is the point here,
    # and exactly why it is never how a deploy gate asks its question. The
    # buffer counts are how a sequential scan announces itself even when the
    # node label is ambiguous, so the measurement carries its own evidence.
    set_config(conn, 'hnsw.ef_search', DEFAULT_EF)
    try:
        analyze_sql, params = module.explain_ann_sql({}, vectors[0], limit,
                                                     None, analyze=True)
        plan = conn.execute(analyze_sql, list(params)).fetchall()
        result['measured_plan'] = '\n'.join(str(row[0]) for row in plan)
        result['measured_plan_ef_search'] = DEFAULT_EF
    except Exception as exc:
        # An operator-facing extra, never the gate: a failure here annotates
        # the report and leaves the timings, which are the deliverable.
        result['measured_plan_error'] = f'{type(exc).__name__}: {scrub(exc)}'
    return result


def print_measurements(rows):
    print()
    print('ef_search  probes    p50_ms    p95_ms    max_ms')
    for row in rows:
        print(f'{row["ef_search"]:>9}  {row["probes"]:>6}  {row["p50_ms"]:>8}  '
              f'{row["p95_ms"]:>8}  {row["max_ms"]:>8}')
    print()
    print(f'Read against the {API_GATEWAY_DEADLINE_S} s API Gateway deadline, '
          f'not the Lambda\'s {LAMBDA_TIMEOUT_S} s timeout: the integration '
          'gives up first,')
    print('and the query still has the lexical arm, RRF and citation assembly '
          'to pay for after this.')
    print('ef_search is the recall dial worth turning -- it moves recall by '
          '1-10%, two orders of')
    print('magnitude more than the fp16 quantization does (recall@10 measured '
          'at 100% over 6 probes,')
    print('max distance delta 1.79e-05).')


# --- step e: resume --------------------------------------------------------

def resume_command(dsn, observed_argv):
    """The exact restart command, printed for the operator and never run.

    Two things this must not do, because --resume-backfill is normally run
    WITHOUT --dsn -- it is the step that comes after --pause-backfill has
    destroyed the environ the DSN was borrowed from:

      * emit an unexpanded ${WS13_DB_ENDPOINT}. Pasted into a shell where
        that variable is unset it becomes an empty host, and libpq then tries
        the local socket -- after the backfill has already been stopped. When
        the host is not known, the command RESOLVES it, the same way
        tools/ws13_ssm.py and the launch-template user data do.
      * hardcode the username. It comes out of the same secret as the
        password; a hardcoded role that no longer matches authenticates as
        the wrong one, or not at all.
    """
    resolve_host = [
        '# the endpoint, resolved rather than assumed (export WS13_DB_HOST to',
        '# override, e.g. when the instance is reached through a tunnel):',
        'HOST="${WS13_DB_HOST:-$(aws rds describe-db-instances \\',
        '      --db-instance-identifier nwmm-ws13 \\',
        '      --query "DBInstances[0].Endpoint.Address" --output text)}"',
        '[ -n "$HOST" ] || echo "resolve the db endpoint before continuing"',
    ]
    if dsn:
        # Only the hostname is echoed; the credential in the DSN never is.
        parsed = urlsplit(dsn if '://' in dsn else f'postgresql://{dsn}')
        if parsed.hostname:
            resolve_host = [f'HOST={parsed.hostname}   # from this run\'s DSN']
    argv = observed_argv[0] if observed_argv else 'python3 ws13_embed_backfill.py'
    return '\n'.join([
        '# restarting the backfill is the operator\'s call; this only prints it.',
        'cd /opt/ws13',
        'export AWS_DEFAULT_REGION=us-west-2',
        'export WS13_BUCKET=nw-mineral-monitor-730883236375',
        *resolve_host,
        f'CRED=$(aws secretsmanager get-secret-value --secret-id {SECRET_ID} \\',
        '      --query SecretString --output text)',
        '# username AND password from the secret: both are what the fleet',
        '# user data authenticates with.',
        'DBUSER=$(printf %s "$CRED" | python3 -c \'import json,sys;'
        'print(json.load(sys.stdin)["username"])\')',
        'PW=$(printf %s "$CRED" | python3 -c \'import json,sys;'
        'print(json.load(sys.stdin)["password"])\')',
        f'export WS13_DB_DSN="postgresql://$DBUSER:$PW@$HOST:{DB_PORT}/'
        f'{DB_NAME}?sslmode=require"',
        'unset CRED PW',
        f'nohup {argv} >> /var/log/ws13-embed.log 2>&1 &',
        '# the backfill resumes by scanning for NULL embedding columns, so it '
        'picks up',
        '# exactly where it stopped; nothing needs to be replayed by hand.',
    ])


# --- main ------------------------------------------------------------------

def write_report(path, report):
    """Atomic so a rerun never reads a half-written report."""
    tmp = f'{path}.tmp'
    with open(tmp, 'w') as fh:
        json.dump(report, fh, indent=2, sort_keys=True)
    os.replace(tmp, path)


def main(argv=None):
    args = parse_args(argv)
    steps = (args.pause_backfill, args.build, args.verify, args.measure,
             args.resume_backfill)
    if not any(steps):
        sys.exit('refusing to run with no step selected: pass --pause-backfill, '
                 '--build, --verify, --measure or --resume-backfill')
    execute = args.yes and not args.dry_run
    report = {'started_at': dt.datetime.now(dt.timezone.utc).isoformat(),
              'host': socket.gethostname(), 'execute': execute, 'steps': [],
              'ok': True}

    needs_db = args.build or args.verify or args.measure
    # The contract gates the DATABASE steps only. Building an index the query
    # cannot use is worse than building none, in that it looks finished -- but
    # --pause-backfill and --resume-backfill neither touch the database nor
    # read a single SQL constant, and gating them on the contract is what made
    # the whole on-host runbook depend on a file layout that does not exist on
    # the host.
    consts, module = None, None
    if needs_db:
        problems = contract.check(Path(args.query_lambda))
        report['contract_problems'] = problems
        if problems:
            report['ok'] = False
            for problem in problems:
                print(f'contract: {problem}')
            return finish(args, report, EXIT_CONTRACT)
        consts = contract.constants(Path(args.query_lambda))
        if args.verify or args.measure:
            # The verifier EXPLAINs what the Lambda itself builds for a given
            # filter set, so it needs the module, not just its constants.
            try:
                module = contract.query_module(Path(args.query_lambda))
            except contract.ContractError as exc:
                report['ok'] = False
                report['contract_problems'] = [str(exc)]
                print(f'contract: {exc}')
                return finish(args, report, EXIT_CONTRACT)

    if args.pause_backfill:
        result = pause_backfill(execute, args.pause_timeout)
        report['steps'].append(result)
        log(f'pause-backfill: {result["note"]}')
        for command in result['commands']:
            print(f'    {command}')
        if not result['ok']:
            report['ok'] = False
            return finish(args, report, EXIT_PAUSE_REFUSED)

    try:
        conn = connect(args.dsn) if needs_db else None
    except DatabaseUnreachable as exc:
        # NOT a plan failure and NOT a contract failure. deploy.sh preflight
        # degrades to a documented skip on this code and stops on every other
        # one, because "unreachable from here" is not evidence about the index.
        report['ok'] = False
        report['unreachable'] = str(exc)
        print(f'WS13 index gate not evaluated: {exc}')
        return finish(args, report, EXIT_UNREACHABLE)
    try:
        if args.build:
            result = build_index(conn, consts, execute)
            report['steps'].append(result)
            log(f'build: {result["note"]}')
            if not result['ok']:
                report['ok'] = False
                return finish(args, report, EXIT_BUILD)

        vectors = []
        if args.verify or args.measure:
            # probe_vectors() runs before either step sets its own bound, and
            # it is the first statement either of them issues.
            set_config(conn, 'statement_timeout', STATEMENT_TIMEOUT_MS)
            vectors = probe_vectors(conn, max(1, args.probes))

        if args.verify:
            result = verify_plan(conn, consts, module, vectors[0], args.limit)
            report['steps'].append(result)
            if not result['ok']:
                report['ok'] = False
                print(f'PLAN ASSERTION FAILED: {"; ".join(result["problems"])}')
                for shape in result['shapes']:
                    if shape.get('problems') and shape.get('plan'):
                        print(f'--- {shape["filters"]} '
                              f'({shape["strategy"]}) ---')
                        print(shape['plan'])
                return finish(args, report, EXIT_PLAN)
            log(f'verify: {len(result["shapes"])} shape(s) plan through '
                f'{consts["INDEX_NAME"]} at ef_search={DEFAULT_EF}')

        if args.measure:
            efs = args.ef_search or list(EF_LADDER)
            result = measure(conn, consts, module, vectors, efs, args.limit)
            report['steps'].append(result)
            if not result['ok']:
                report['ok'] = False
                log(f'measure: {result["note"]}')
                return finish(args, report, EXIT_PLAN)
            print_measurements(result['rows'])
    finally:
        if conn is not None:
            conn.close()

    if args.resume_backfill:
        observed = []
        for step in report['steps']:
            observed.extend(step.get('observed_argv', []))
        command = resume_command(args.dsn, observed)
        report['resume_command'] = command
        print()
        print(command)

    return finish(args, report, EXIT_OK)


def finish(args, report, code):
    report['finished_at'] = dt.datetime.now(dt.timezone.utc).isoformat()
    report['exit_code'] = code
    if args.report:
        write_report(args.report, report)
    # stdout too: over SSM it is the only thing that comes back.
    print('--- ws13 ann index report (json) ---')
    print(json.dumps(report, indent=2, sort_keys=True))
    return code


if __name__ == '__main__':
    sys.exit(main())
