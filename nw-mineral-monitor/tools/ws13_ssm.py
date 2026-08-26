#!/usr/bin/env python3
"""Run a local read-only Python script on the in-VPC WS13 host, via SSM.

There is no direct route to the database. RDS nwmm-ws13 sits in a private VPC
with no public endpoint, and policy blocks retrieving the DB secret to a local
machine, so nothing on a laptop can connect and nothing on a laptop should
ever hold the credential. The pattern that works, and the one this tool
packages: send a script to the in-VPC host, let it borrow the DSN from the
running backfill's own environment, and read only the script's stdout back.
The secret never leaves the instance and is never printed. Anything
DSN-shaped in the returned output is redacted here as well, belt and braces.

    # see exactly what would be sent, send nothing
    tools/ws13_ssm.py --script /tmp/counts.py --with-dsn --dry-run

    # actually send it (never happens without --yes)
    tools/ws13_ssm.py --script /tmp/counts.py --with-dsn --yes

--with-dsn prepends a preamble that opens `conn` for the script: first from
the environ of the running ws13_embed_backfill.py, and if that process has
exited, by re-deriving the DSN on-host from Secrets Manager (ws13/postgres)
with the instance role, exactly as the launch-template user data does. After
pipelines/ws13_build_ann_index.py --pause-backfill there is no backfill left
to borrow from, so that fallback is the only path and is not optional.

The script is sent as one stdin module: it runs with no repository siblings
on sys.path, so multi-module tools have to be unpacked from the bundle on the
host instead. Phase A is three files and needs all three in
ws13/fleet/bundle.tar.gz -- ws13_build_ann_index.py, ws13_index_contract.py
and ws13_query_lambda.py, which is the single source of truth for the halfvec
constants and for the filtered probe the plan gate EXPLAINs. The bundle is
untarred FLAT into /opt/ws13, and both modules resolve their siblings from
beside themselves, so no --query-lambda argument is needed there. This tool
is for short, self-contained, read-only probes.

Operator note: this sends a command to a live host, so it refuses to do
anything without an explicit --yes, and the local permission classifier
refuses some of these invocations outright. Three refusals means the command
goes to the operator -- run with --dry-run --emit-command and hand over the
printed `aws ssm send-command` line. Do not go hunting for a phrasing that
slips past the classifier.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
from pathlib import Path
import re
import sys
import time

import boto3


DEFAULT_INSTANCE = 'i-0818521a8b3ff7c90'
DEFAULT_REGION = os.environ.get('AWS_DEFAULT_REGION', 'us-west-2')
DOCUMENT = 'AWS-RunShellScript'
SECRET_ID = 'ws13/postgres-7wq3XL'
DB_INSTANCE = 'nwmm-ws13'
DB_STACK = 'ws13-dataplane'
DB_NAME = 'nwmm'
DB_PORT = '5432'
BACKFILL_MARKER = 'ws13_embed_backfill'
TERMINAL = ('Success', 'Cancelled', 'Failed', 'TimedOut', 'Undeliverable',
            'Terminated')
# Conservative, not the documented ceiling: the whole send-command request is
# bounded, and a script anywhere near this size belongs in the S3 bundle.
MAX_B64_CHARS = 45000
# get_command_invocation returns only the first 24,000 characters of each
# stream; anything longer has to be written to S3 by the script itself.
SSM_OUTPUT_LIMIT = 24000
DSN_RE = re.compile(r'postgres(?:ql)?://[^\s\'"]+')
PASSWORD_RE = re.compile(r'("password"\s*:\s*")[^"]*(")')
# A local script that echoes any of these is refused before it is sent. The
# host can print whatever it likes; this is the one place we can still stop it.
SECRET_TOKENS = ('WS13_DB_DSN', 'SecretString', 'get-secret-value', 'conn.info')

DSN_PREAMBLE = r'''
# --- ws13_ssm.py --with-dsn preamble -------------------------------------
# Opens `conn` and never prints the DSN. First choice is to borrow it from
# the running backfill's own environment, so the secret is never fetched,
# never written down and never leaves the instance. The fallback re-derives
# it on-host from Secrets Manager with the instance role, the same way the
# launch-template user data does, for after the backfill has exited.
import glob
import json
import os
import subprocess

import psycopg

_SECRET_ID = '__SECRET_ID__'
_DB_INSTANCE = '__DB_INSTANCE__'
_DB_STACK = '__DB_STACK__'
_DB_NAME = '__DB_NAME__'
_DB_PORT = '__DB_PORT__'


def _dsn_from_backfill():
    for path in glob.glob('/proc/[0-9]*/cmdline'):
        try:
            argv = open(path, 'rb').read().decode('utf-8', 'replace')
        except OSError:
            continue                      # exited between the glob and the read
        if '__BACKFILL__' not in argv:
            continue
        pid = path.split('/')[2]
        try:
            raw = open('/proc/%s/environ' % pid, 'rb').read()
        except OSError:
            continue
        env = dict(x.split(b'=', 1) for x in raw.split(b'\0') if b'=' in x)
        if env.get(b'WS13_DB_DSN'):
            return env[b'WS13_DB_DSN'].decode()
    return None


def _aws(*args):
    done = subprocess.run(('aws',) + args, capture_output=True, text=True)
    value = done.stdout.strip()
    if done.returncode != 0 or value in ('', 'None'):
        return None
    return value


def _db_endpoint():
    return (os.environ.get('WS13_DB_HOST')
            or _aws('rds', 'describe-db-instances',
                    '--db-instance-identifier', _DB_INSTANCE,
                    '--query', 'DBInstances[0].Endpoint.Address',
                    '--output', 'text')
            or _aws('cloudformation', 'describe-stacks',
                    '--stack-name', _DB_STACK, '--query',
                    "Stacks[0].Outputs[?OutputKey=='DbEndpoint'].OutputValue|[0]",
                    '--output', 'text'))


def _dsn_from_secret():
    raw = _aws('secretsmanager', 'get-secret-value', '--secret-id', _SECRET_ID,
               '--query', 'SecretString', '--output', 'text')
    if not raw:
        raise SystemExit('cannot read %s with this instance role' % _SECRET_ID)
    secret = json.loads(raw)
    host = _db_endpoint()
    if not host:
        raise SystemExit('cannot resolve the db endpoint; export WS13_DB_HOST')
    return ('postgresql://%s:%s@%s:%s/%s?sslmode=require'
            % (secret['username'], secret['password'], host, _DB_PORT, _DB_NAME))


_dsn = _dsn_from_backfill() or _dsn_from_secret()
conn = psycopg.connect(_dsn, autocommit=True)
del _dsn
# --- end preamble: the script below runs with `conn` already open ---------
'''


def preamble():
    """The --with-dsn preamble with its placeholders filled in."""
    text = DSN_PREAMBLE
    for token, value in (('__SECRET_ID__', SECRET_ID),
                         ('__DB_INSTANCE__', DB_INSTANCE),
                         ('__DB_STACK__', DB_STACK),
                         ('__DB_NAME__', DB_NAME),
                         ('__DB_PORT__', DB_PORT),
                         ('__BACKFILL__', BACKFILL_MARKER)):
        text = text.replace(token, value)
    leftover = re.search(r'__[A-Z_]+__', text)
    if leftover:
        raise SystemExit(f'preamble placeholder {leftover.group(0)} unfilled')
    return text


def redact(text):
    """Strip anything DSN- or password-shaped out of returned output."""
    if not text:
        return ''
    text = DSN_RE.sub('postgresql://[redacted]', text)
    return PASSWORD_RE.sub(r'\1[redacted]\2', text)


def secret_echoes(script_text):
    """Lines in the local script that would print the credential."""
    problems = []
    for number, line in enumerate(script_text.splitlines(), 1):
        if 'print' in line and any(token in line for token in SECRET_TOKENS):
            problems.append(f'line {number}: {line.strip()}')
    return problems


def build_command(script_text, with_dsn):
    """(shell command, payload) for AWS-RunShellScript."""
    payload = (preamble() + '\n' + script_text) if with_dsn else script_text
    encoded = base64.b64encode(payload.encode('utf-8')).decode('ascii')
    if len(encoded) > MAX_B64_CHARS:
        raise SystemExit(
            f'payload is {len(encoded)} base64 chars, over the {MAX_B64_CHARS} '
            'this tool will send; put it in the S3 bundle instead')
    return f'echo {encoded} | base64 -d | python3 -', payload


def cli_equivalent(command, instance, region, timeout, parameter_file):
    """The exact aws CLI line, for handing to an operator."""
    source = (f'file://{parameter_file}' if parameter_file
              else "'<see --emit-command>'")
    return (f'aws ssm send-command --region {region} '
            f'--instance-ids {instance} --document-name {DOCUMENT} '
            f'--parameters {source}'
            + ('' if parameter_file else f'  # commands=[{command[:40]}...], '
                                         f'executionTimeout={timeout}'))


def parameters(command, timeout):
    # TimeoutSeconds on send_command bounds *delivery* -- how long the command
    # may sit unstarted. executionTimeout bounds the run itself. Setting only
    # the first is the usual mistake: the run then silently inherits the
    # document default of 3600 s.
    return {'commands': [command], 'executionTimeout': [str(timeout)]}


def poll(ssm, command_id, instance, timeout, interval):
    """Wait for a terminal invocation status."""
    deadline = time.time() + timeout + 60
    invocation = None
    while time.time() < deadline:
        try:
            invocation = ssm.get_command_invocation(CommandId=command_id,
                                                    InstanceId=instance)
        except ssm.exceptions.InvocationDoesNotExist:
            # send_command is eventually consistent; the invocation shows up a
            # beat later.
            time.sleep(interval)
            continue
        if invocation['Status'] in TERMINAL:
            return invocation
        time.sleep(interval)
    return invocation


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--script', required=True,
                   help='local .py file to run on the instance')
    p.add_argument('--with-dsn', action='store_true',
                   help='prepend the preamble that opens `conn` on the host')
    p.add_argument('--instance', default=DEFAULT_INSTANCE)
    p.add_argument('--region', default=DEFAULT_REGION)
    p.add_argument('--timeout', type=int, default=900,
                   help='seconds the remote script may run (default 900)')
    p.add_argument('--poll-interval', type=int, default=5)
    p.add_argument('--comment', default='ws13 read-only probe',
                   help='SSM command comment (truncated to 100 chars)')
    p.add_argument('--emit-command',
                   help='write the exact --parameters JSON here for a human')
    p.add_argument('--dry-run', action='store_true',
                   help='print what would be sent and send nothing')
    p.add_argument('--yes', action='store_true',
                   help='required to actually send to a live host')
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    path = Path(args.script)
    if not path.is_file():
        sys.exit(f'no such script: {path}')
    script_text = path.read_text(encoding='utf-8')

    echoes = secret_echoes(script_text)
    if echoes:
        print('refusing to send: the script prints something credential-shaped')
        for echo in echoes:
            print(f'  {echo}')
        return 2

    command, payload = build_command(script_text, args.with_dsn)
    if args.emit_command:
        Path(args.emit_command).write_text(
            json.dumps(parameters(command, args.timeout)), encoding='utf-8')

    if args.dry_run or not args.yes:
        print(f'instance : {args.instance}')
        print(f'region   : {args.region}')
        print(f'document : {DOCUMENT}')
        print(f'exec time: {args.timeout}s')
        print(f'payload  : {len(payload)} chars, {len(command)} base64 chars, '
              f'with_dsn={args.with_dsn}')
        print(cli_equivalent(command, args.instance, args.region, args.timeout,
                             args.emit_command))
        print('--- script as it would run on the host ---')
        print(payload)
        if not args.yes:
            print('refusing to send without --yes (this reaches a live host)')
            return 0 if args.dry_run else 2
        return 0

    ssm = boto3.client('ssm', region_name=args.region)
    sent = ssm.send_command(InstanceIds=[args.instance], DocumentName=DOCUMENT,
                            Comment=args.comment[:100],
                            TimeoutSeconds=max(30, args.timeout),
                            Parameters=parameters(command, args.timeout))
    command_id = sent['Command']['CommandId']
    print(f'ssm instance={args.instance} command={command_id}')

    invocation = poll(ssm, command_id, args.instance, args.timeout,
                      args.poll_interval)
    if invocation is None or invocation['Status'] not in TERMINAL:
        print(f'no terminal status within {args.timeout}s; the command may '
              'still be running. Fetch it later with:')
        print(f'aws ssm get-command-invocation --region {args.region} '
              f'--command-id {command_id} --instance-id {args.instance}')
        return 3

    stdout = redact(invocation.get('StandardOutputContent', ''))
    stderr = redact(invocation.get('StandardErrorContent', ''))
    print(f'status: {invocation["Status"]} '
          f'rc={invocation.get("ResponseCode")}')
    if stdout:
        print('--- stdout ---')
        print(stdout)
    if stderr:
        print('--- stderr ---')
        print(stderr)
    for name, stream in (('stdout', stdout), ('stderr', stderr)):
        if len(stream) >= SSM_OUTPUT_LIMIT:
            print(f'({name} hit the {SSM_OUTPUT_LIMIT}-char SSM limit and is '
                  'truncated; have the script write to S3 instead)')
    return 0 if invocation['Status'] == 'Success' else 1


if __name__ == '__main__':
    sys.exit(main())
