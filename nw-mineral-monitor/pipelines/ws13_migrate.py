#!/usr/bin/env python3
"""Apply (or verify) pipelines/ws13_migrations.sql against the WS13 database.

Why this exists as a runner rather than a psql one-liner:

  * the STORED generated columns (admission_class, doc_year_min,
    doc_year_max) rewrite all 56,282 ws13_documents rows under ACCESS
    EXCLUSIVE, so "did it work?" needs a per-statement row count and elapsed
    time, not a wall of `ALTER TABLE`;
  * the whole file has to land or none of it: a half-applied migration leaves
    ws13_documents with an admission_class but no rights columns, which reads
    to the retrieval Lambda as "this document has no licence" rather than as
    an error. Everything runs in one transaction and rolls back together;
  * the ws13_reader password must never reach a laptop, a git object, or the
    RDS server log. --reader-secret-arn fetches it from Secrets Manager on the
    in-VPC host and installs the SCRAM-SHA-256 verifier -- not the cleartext
    -- in the same statement that promotes the role to LOGIN. Without the flag
    the role stays NOLOGIN, which is stated loudly rather than assumed;
  * --check is the read-only gate the deploy preflight calls. It verifies the
    columns and their generation expressions, the indexes, the year helpers,
    the county function, the role, the role's privileges, and that every
    stored year range still agrees with the helper that generates it, and
    exits 1 on any gap. --require-provenance additionally proves that
    pipelines/ws13_backfill_provenance.py has run: no licensed or research
    copy without a rights_basis, and source_url coverage above a threshold.
    That gate belongs in front of the WS13 retrieval flag, because
    infra/ws13_query_lambda.py raises rather than emit a citation for a
    document whose rights it cannot state.

Usage:

    ws13_migrate.py --dry-run                 # print the statements, run none
    ws13_migrate.py --apply                   # apply, report rows + seconds
    ws13_migrate.py --apply --reader-secret-arn arn:aws:secretsmanager:...
    ws13_migrate.py --check                   # deploy preflight gate
    ws13_migrate.py --check --require-provenance   # ... before enabling WS13
"""
import argparse
import base64
import hashlib
import hmac
import json
import os
import re
import sys
import time

import psycopg

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_SQL = os.path.join(HERE, 'ws13_migrations.sql')

READER_ROLE = 'ws13_reader'
# The tables the retrieval path reads. ws13_mine_id_map is deliberately absent:
# it is built by a later stage and its grant is guarded in the .sql.
READER_TABLES = ('ws13_documents', 'ws13_pages', 'ws13_chunks', 'ws13_manifest')
WRITE_PRIVILEGES = ('INSERT', 'UPDATE', 'DELETE', 'TRUNCATE')
REQUIRED_COLUMNS = ('admission_class', 'doc_year_min', 'doc_year_max',
                    'source_url', 'rights_basis', 'public_domain')
REQUIRED_INDEXES = ('ws13_documents_admission', 'ws13_documents_years',
                    'ws13_documents_county_key')
# The IMMUTABLE helpers the year columns are generated from. Both must be
# IMMUTABLE or PostgreSQL would not accept them in a generated column at all.
YEAR_FUNCTIONS = ('ws13_doc_year_min', 'ws13_doc_year_max')

# column -> the generation expression it must have, normalised by
# _normalized_generation(). Mirrors the $generated_guard$ block in
# ws13_migrations.sql, which is the one that fails the migration; this copy is
# what makes --check fail on a column an operator altered by hand afterwards.
# Substrings are not enough: 'split_part' and 's3_key' both appear in
# split_part(s3_key, '/', 3), which yields the portal segment ('azgs_admmr')
# rather than the rights class, and every citation for such a row would be
# refused by infra/ws13_query_lambda.py as an unknown admission_class.
GENERATED_COLUMNS = {
    'admission_class': "split_part(s3_key,'/',2)",
    'doc_year_min': 'ws13_doc_year_min(doc_date)',
    'doc_year_max': 'ws13_doc_year_max(doc_date)',
}

# The year gate is no longer a second copy of the backfill predicate -- there
# is no backfill. doc_year_min/doc_year_max are GENERATED ALWAYS ... STORED
# over ws13_doc_year_min()/ws13_doc_year_max(), so this asks whether every
# stored value still equals what the helper produces today. It answers "the
# columns are maintained" (no row can be missed by a pass nobody ran) and
# "the helper was not edited out from under the stored values" in one query,
# and it cannot drift from the definition because it calls the definition.
YEAR_GAP_SQL = """
SELECT count(*) FROM ws13_documents d
 WHERE d.doc_year_min IS DISTINCT FROM ws13_doc_year_min(d.doc_date)
    OR d.doc_year_max IS DISTINCT FROM ws13_doc_year_max(d.doc_date)
"""

# One scan of ws13_documents (56,282 rows of small metadata) for every number
# the provenance gate needs. 'originals' are public-domain, so rights_basis is
# only demanded for the 13,013 licensed AZGS copies and the 32,312 IGS
# research copies -- those are the ones whose citation carries a licence.
PROVENANCE_SQL = """
SELECT count(*) AS documents,
       count(*) FILTER (
           WHERE admission_class IN ('licensed-copies', 'research-copies')
             AND (rights_basis IS NULL OR rights_basis = '')) AS no_rights,
       count(*) FILTER (
           WHERE admission_class IS NULL
              OR admission_class NOT IN ('originals', 'licensed-copies',
                                         'research-copies')) AS unknown_class,
       count(*) FILTER (
           WHERE source_url IS NULL OR source_url = '') AS no_source_url
  FROM ws13_documents
"""
# The WS12 harvest manifest carries a source_url on all 106,396 rows, so a
# completed backfill lands at ~100%. The default leaves room for the handful
# of documents that reached ws13_documents from outside that manifest without
# letting a backfill that never ran (0%) pass.
MIN_SOURCE_URL_COVERAGE = 0.99

# PostgreSQL's own defaults for a SCRAM verifier (scram_build_verifier()).
SCRAM_ITERATIONS = 4096
SCRAM_SALT_BYTES = 16

_DOLLAR_TAG = re.compile(r'\$[A-Za-z_][A-Za-z0-9_]*\$|\$\$')
_WHITESPACE = re.compile(r'\s+')


def split_statements(sql):
    """Split a SQL script into executable statements.

    A naive split on ';' cuts every `DO $tag$ ... END $tag$;` block in
    ws13_migrations.sql in half, and would also split inside the 'ws13\\_%'
    LIKE pattern if that ever grew a semicolon. This walks the script once,
    tracking line comments, nested block comments, single-quoted literals,
    double-quoted identifiers and dollar-quoted bodies with arbitrary tags.

    Returns a list of {'sql', 'label'} dicts; 'label' is the statement with
    its comments and redundant whitespace removed, for progress reporting.
    """
    statements = []
    body, code = [], []
    index, length, block_depth = 0, len(sql), 0
    while index < length:
        char = sql[index]
        if block_depth:
            # PostgreSQL block comments nest, unlike C's.
            if sql.startswith('/*', index):
                block_depth += 1
                body.append('/*')
                index += 2
                continue
            if sql.startswith('*/', index):
                block_depth -= 1
                body.append('*/')
                index += 2
                continue
            body.append(char)
            index += 1
            continue
        if sql.startswith('--', index):
            stop = sql.find('\n', index)
            stop = length if stop < 0 else stop
            body.append(sql[index:stop])
            index = stop
            continue
        if sql.startswith('/*', index):
            block_depth = 1
            body.append('/*')
            index += 2
            continue
        if char in ("'", '"'):
            stop = _scan_quoted(sql, index, char)
            body.append(sql[index:stop])
            code.append(sql[index:stop])
            index = stop
            continue
        if char == '$':
            match = _DOLLAR_TAG.match(sql, index)
            if match:
                tag = match.group(0)
                stop = sql.find(tag, match.end())
                stop = length if stop < 0 else stop + len(tag)
                body.append(sql[index:stop])
                code.append(sql[index:stop])
                index = stop
                continue
        if char == ';':
            _emit(statements, body, code)
            body, code = [], []
            index += 1
            continue
        body.append(char)
        code.append(char)
        index += 1
    _emit(statements, body, code)
    return statements


def _scan_quoted(sql, start, quote):
    """Index just past the closing quote. '' and "" are embedded quotes.

    standard_conforming_strings is on in PostgreSQL 9.1+, so a backslash
    inside a literal is an ordinary character and must not be treated as an
    escape -- ws13_migrations.sql relies on that for '\\d{4}' and 'ws13\\_%'.
    """
    index = start + 1
    while index < len(sql):
        if sql[index] == quote:
            if index + 1 < len(sql) and sql[index + 1] == quote:
                index += 2
                continue
            return index + 1
        index += 1
    return len(sql)


def _emit(statements, body, code):
    text = ''.join(body).strip()
    label = ' '.join(''.join(code).split())
    if not label:
        # Trailing whitespace or a comment-only tail: nothing to execute.
        return
    statements.append({'sql': text, 'label': label})


def apply_migrations(conn, statements, echo=True):
    """Run every statement in one transaction. Returns per-statement timings.

    conn must be a non-autocommit connection: the caller commits, so a failure
    anywhere rolls the whole migration back rather than leaving ws13_documents
    with half the citation contract's columns.
    """
    report = []
    for number, statement in enumerate(statements, 1):
        started = time.perf_counter()
        cursor = conn.execute(statement['sql'])
        elapsed = time.perf_counter() - started
        # DDL reports rowcount -1; only the backfill UPDATE has a real count.
        rows = cursor.rowcount if cursor.rowcount is not None else -1
        report.append({'label': statement['label'], 'rows': rows,
                       'seconds': round(elapsed, 3)})
        if echo:
            shown = 'rows=-' if rows < 0 else f'rows={rows}'
            print(f'  [{number:2d}/{len(statements)}] {elapsed:7.3f}s '
                  f'{shown:<12} {_short(statement["label"])}')
    return report


def _short(label, width=70):
    return label if len(label) <= width else label[:width - 3] + '...'


def reader_password(secret_arn, region):
    """Read the ws13_reader password from Secrets Manager.

    Imported lazily so --check and --dry-run need no AWS credentials and no
    boto3 on the host running the gate.
    """
    import boto3
    client = boto3.client('secretsmanager', region_name=region)
    raw = client.get_secret_value(SecretId=secret_arn).get('SecretString')
    if not raw:
        raise SystemExit(f'{secret_arn} has no SecretString')
    try:
        payload = json.loads(raw)
    except ValueError:
        return raw.strip()
    if not isinstance(payload, dict):
        return raw.strip()
    # SPECIFIC NAMES FIRST, and the order is the whole point. This function
    # accepts either a dedicated reader secret (whose password is naturally
    # under 'password') or the main ws13/postgres secret carrying an extra
    # reader key -- which is what ws13_retrieval.yaml expects, since it
    # resolves {{...:SecretString:ws13_reader_password}} out of DbSecretArn.
    # With 'password' checked first, pointing --reader-secret-arn at
    # ws13/postgres found the RDS MASTER credential and would have set
    # ws13_reader's password to it: the role created SELECT-only and verified
    # to hold no write privileges, handed the superuser's password. There is
    # exactly one secret in this account, so that was not a corner case, it
    # was the only way the documented command could run.
    for key in ('ws13_reader_password', 'reader_password'):
        value = payload.get(key)
        if value:
            return str(value)
    # Last resort, and only meaningful for a secret created FOR the reader. If
    # this secret also carries a 'username', it is a database credential pair
    # for some other role -- almost certainly the master -- and reusing it here
    # would be the defect above rather than a convenience.
    value = payload.get('password')
    if value and payload.get('username'):
        raise SystemExit(
            f'{secret_arn} has a username/password pair but no reader key. '
            f"That pair is another role's credential (the master, for "
            f'ws13/postgres); reusing it would give ws13_reader those '
            f'privileges. Add a {READER_ROLE}_password key to this secret, or '
            f'point --reader-secret-arn at a secret created for the reader.')
    if value:
        return str(value)
    # Names only, never values: this message can end up in a deploy log.
    raise SystemExit(f'{secret_arn} has no password field; keys present: '
                     f'{sorted(payload)}')


def scram_sha_256_verifier(password, salt=None, iterations=SCRAM_ITERATIONS):
    """The PostgreSQL SCRAM-SHA-256 verifier for `password`.

    The defect this closes: `ALTER ROLE ws13_reader LOGIN PASSWORD
    '<cleartext>'` is DDL, and PostgreSQL does not redact statement text. An
    RDS parameter group with log_statement = 'ddl' (a routine audit setting)
    writes the cleartext password to the postgres error log, which RDS ships
    to CloudWatch Logs and keeps; pg_stat_activity.query shows the same
    statement to anyone holding pg_read_all_stats while it runs. Fetching the
    secret on an in-VPC host keeps it off laptops and out of git, which is
    what the old docstring reasoned about, and does nothing about either of
    those two exposures.

    A verifier is exactly what pg_authid stores, so logging it discloses
    nothing that reading pg_authid would not, and PostgreSQL 10+ accepts one
    in place of a password.

    RFC 5802 section 3, which is also PostgreSQL's scram_build_verifier():

        SaltedPassword = PBKDF2-HMAC-SHA256(password, salt, iterations, 32)
        StoredKey      = SHA256(HMAC(SaltedPassword, 'Client Key'))
        ServerKey      = HMAC(SaltedPassword, 'Server Key')

    rendered as SCRAM-SHA-256$<iterations>:<salt>$<StoredKey>:<ServerKey>
    with the three byte strings base64-encoded.

    ASCII passwords only: the authenticating client SASLprep-normalises the
    password, this does not implement SASLprep, and a mismatch there would
    show up as an unauthenticable role rather than as an error here.
    """
    if not password:
        raise SystemExit('refusing to install an empty ws13_reader password')
    try:
        encoded = password.encode('ascii')
    except UnicodeEncodeError:
        raise SystemExit(
            'the ws13_reader password is not ASCII and this script pre-hashes '
            'it (SCRAM-SHA-256) without implementing SASLprep. Rotate the '
            'secret to an ASCII password, or pass --plaintext-password and '
            'accept that log_statement can capture it.')
    salt = os.urandom(SCRAM_SALT_BYTES) if salt is None else salt
    salted = hashlib.pbkdf2_hmac('sha256', encoded, salt, iterations, 32)
    client_key = hmac.new(salted, b'Client Key', hashlib.sha256).digest()
    stored_key = hashlib.sha256(client_key).digest()
    server_key = hmac.new(salted, b'Server Key', hashlib.sha256).digest()
    return 'SCRAM-SHA-256${}:{}${}:{}'.format(
        iterations,
        base64.b64encode(salt).decode('ascii'),
        base64.b64encode(stored_key).decode('ascii'),
        base64.b64encode(server_key).decode('ascii'))


def install_reader_password(conn, password, plaintext=False):
    """Promote ws13_reader to LOGIN and set its password in one statement.

    What travels to the server is the SCRAM-SHA-256 verifier, not the
    password -- see scram_sha_256_verifier() for why the cleartext form is a
    log-disclosure bug. psycopg cannot bind a parameter into ALTER ROLE, so
    the literal is quoted server-side by psycopg.sql.Literal rather than by
    string formatting. Returns what was sent, for the operator's log line.
    """
    from psycopg import sql
    secret = password if plaintext else scram_sha_256_verifier(password)
    conn.execute(sql.SQL('ALTER ROLE {} LOGIN PASSWORD {}').format(
        sql.Identifier(READER_ROLE), sql.Literal(secret)))
    return 'cleartext password' if plaintext else 'SCRAM-SHA-256 verifier'


def verify_reader_login(dsn, password):
    """Connect as ws13_reader with `password` and run SELECT 1.

    A pre-hashed password has no visible failure mode at install time:
    PostgreSQL stores whatever verifier it is handed, and a wrong one only
    surfaces later as a retrieval Lambda that cannot connect. One connection
    from the same in-VPC host turns that into an immediate local error.

    The admin DSN supplies host/port/dbname/sslmode; only the credentials are
    replaced. passfile and service are dropped so neither can override them.
    """
    from psycopg.conninfo import conninfo_to_dict, make_conninfo
    params = conninfo_to_dict(dsn)
    for key in ('passfile', 'service', 'sslpassword'):
        params.pop(key, None)
    params['user'] = READER_ROLE
    params['password'] = password
    with psycopg.connect(make_conninfo(**params), connect_timeout=10) as conn:
        conn.execute('SELECT 1').fetchone()


def _one(conn, query, params=None):
    row = conn.execute(query, params).fetchone()
    return row[0] if row else None


def _normalized_generation(expression):
    """A generation expression stripped of the noise pg_get_expr adds.

    pg_get_expr renders split_part(s3_key, '/', 2) with an explicit
    '::text' cast on the literal and its own spacing, and schema-qualifies
    the function when public is not in search_path. None of that changes the
    meaning; the field index does, which is what GENERATED_COLUMNS pins.
    """
    if expression is None:
        return None
    collapsed = _WHITESPACE.sub('', expression)
    return collapsed.replace('::text', '').replace('public.', '')


def run_checks(conn, require_login=False, require_provenance=False,
               min_source_url_coverage=MIN_SOURCE_URL_COVERAGE):
    """Read-only verification. Returns [(name, ok, detail)] in report order.

    require_provenance turns the three provenance measurements from advisory
    into failures. They are advisory by default because --apply legitimately
    runs before pipelines/ws13_backfill_provenance.py; they must be demanded
    before the WS13 retrieval path is enabled, because a hit on a licensed or
    research copy with no rights_basis is not a degraded citation, it is a
    RuntimeError out of infra/ws13_query_lambda.py.
    """
    results = []

    def record(name, ok, detail=''):
        results.append((name, bool(ok), detail))

    def record_gate(name, ok, detail):
        # Same shape the ws13_reader login state uses: always measured and
        # always printed, only fatal when the caller asked for it.
        if require_provenance:
            record(name, ok, '' if ok else detail)
        else:
            record(name, True, f'{detail} [advisory; --require-provenance '
                               f'makes this a failure]')

    present = {row[0] for row in conn.execute(
        "SELECT a.attname FROM pg_attribute a "
        " WHERE a.attrelid = to_regclass('public.ws13_documents') "
        "   AND a.attnum > 0 AND NOT a.attisdropped").fetchall()}
    for column in REQUIRED_COLUMNS:
        record(f'column ws13_documents.{column}', column in present,
               '' if column in present else 'missing')

    # attgenerated = 's' alone was too weak a check: it accepts a STORED
    # column generated from the wrong expression, which for admission_class
    # is a rights error and for the year columns is a silently wrong filter.
    generation = {row[0]: row[1] for row in conn.execute(
        "SELECT a.attname, pg_get_expr(d.adbin, d.adrelid) "
        "  FROM pg_attribute a "
        "  LEFT JOIN pg_attrdef d "
        "    ON d.adrelid = a.attrelid AND d.adnum = a.attnum "
        " WHERE a.attrelid = to_regclass('public.ws13_documents') "
        "   AND a.attgenerated = 's' AND NOT a.attisdropped").fetchall()}
    for column, expected in sorted(GENERATED_COLUMNS.items()):
        actual = _normalized_generation(generation.get(column))
        ok = actual in (expected, f'({expected})')
        record(f'{column} is STORED generated from {expected}', ok,
               '' if ok else ('not a STORED generated column'
                              if actual is None else f'generated from {actual}'))

    indexes = {row[0] for row in conn.execute(
        "SELECT indexname FROM pg_indexes "
        " WHERE schemaname = 'public' AND indexname = ANY(%s)",
        (list(REQUIRED_INDEXES),)).fetchall()}
    for name in REQUIRED_INDEXES:
        record(f'index {name}', name in indexes,
               '' if name in indexes else 'missing')

    functions = {row[0]: row[1] for row in conn.execute(
        'SELECT p.proname, p.provolatile FROM pg_proc p '
        ' JOIN pg_namespace n ON n.oid = p.pronamespace '
        " WHERE n.nspname = 'public' "
        '   AND p.proname = ANY(%s) '
        '   AND p.pronargs = 1 '
        "   AND p.proargtypes[0] = 'text'::regtype",
        (['ws13_county_key'] + list(YEAR_FUNCTIONS),)).fetchall()}
    for name in ('ws13_county_key',) + YEAR_FUNCTIONS:
        volatility = functions.get(name)
        record(f'function {name}(text) IMMUTABLE', volatility == 'i',
               '' if volatility == 'i' else
               'missing' if volatility is None else
               f'provolatile={volatility!r}')

    can_login = _one(conn, 'SELECT rolcanlogin FROM pg_roles WHERE rolname = %s',
                     (READER_ROLE,))
    role_exists = can_login is not None
    record(f'role {READER_ROLE}', role_exists, '' if role_exists else 'missing')
    if role_exists:
        # Informational unless the caller demands a usable login: the password
        # is installed separately, by --reader-secret-arn on an in-VPC host.
        detail = ('LOGIN' if can_login else
                  'NOLOGIN - run --apply --reader-secret-arn on an in-VPC host')
        if require_login:
            record(f'role {READER_ROLE} can log in', can_login, detail)
        else:
            record(f'role {READER_ROLE} login state', True, detail)
        # has_table_privilege raises on a missing relation, so establish which
        # tables exist first: a gate must report a gap, not crash on one.
        existing = {row[0] for row in conn.execute(
            "SELECT tablename FROM pg_tables "
            " WHERE schemaname = 'public' AND tablename = ANY(%s)",
            (list(READER_TABLES),)).fetchall()}
        for table in READER_TABLES:
            if table not in existing:
                record(f'{READER_ROLE} SELECT on {table}', False,
                       'table does not exist')
                continue
            granted = _one(conn, 'SELECT has_table_privilege(%s, %s, %s)',
                           (READER_ROLE, table, 'SELECT'))
            record(f'{READER_ROLE} SELECT on {table}', granted,
                   '' if granted else 'not granted')
        writable = [
            f'{table}:{privilege}'
            for table in sorted(existing) for privilege in WRITE_PRIVILEGES
            if _one(conn, 'SELECT has_table_privilege(%s, %s, %s)',
                    (READER_ROLE, table, privilege))]
        record(f'{READER_ROLE} has no write privileges', not writable,
               ', '.join(writable))

    year_ready = ({'doc_year_min', 'doc_year_max'} <= present
                  and all(functions.get(name) == 'i'
                          for name in YEAR_FUNCTIONS))
    if year_ready:
        # Both scanning checks below are guarded: a gate that crashes on a
        # statement_timeout reports nothing, and "the query did not finish"
        # is a gap like any other.
        try:
            gap = _one(conn, YEAR_GAP_SQL)
        except Exception as exc:                       # noqa: BLE001 - gate
            record('doc_year_min/max agree with the helpers', False,
                   f'query failed: {exc}')
        else:
            record('doc_year_min/max agree with the helpers', gap == 0,
                   '' if gap == 0 else
                   f'{gap} documents have a stored year range the helper '
                   f'does not reproduce; rerun --apply')

    if {'rights_basis', 'source_url', 'admission_class'} <= present:
        try:
            row = conn.execute(PROVENANCE_SQL).fetchone()
        except Exception as exc:                       # noqa: BLE001 - gate
            record('provenance backfill complete', False,
                   f'query failed: {exc}')
        else:
            documents, no_rights, unknown_class, no_source_url = row
            covered = documents - no_source_url
            coverage = covered / documents if documents else 1.0
            record_gate(
                'licensed/research copies carry a rights_basis',
                no_rights == 0,
                f'{no_rights} of {documents} documents in licensed-copies or '
                f'research-copies have no rights_basis; run '
                f'ws13_backfill_provenance.py --require-complete')
            record_gate(
                f'source_url coverage >= {min_source_url_coverage:.0%}',
                coverage >= min_source_url_coverage,
                f'{covered}/{documents} rows have a source_url '
                f'({coverage:.2%}); the WS12 manifest can fill the rest')
            record_gate(
                'every admission_class is a known rights class',
                unknown_class == 0,
                f'{unknown_class} of {documents} documents have an '
                f'admission_class outside originals/licensed-copies/'
                f'research-copies; every citation for them is refused')
    return results


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--apply', action='store_true',
                      help='run the migration in one transaction')
    mode.add_argument('--check', action='store_true',
                      help='verify only, write nothing, exit 1 on any gap')
    mode.add_argument('--dry-run', action='store_true',
                      help='print the statements that --apply would run')
    parser.add_argument('--dsn', default=os.environ.get('WS13_DB_DSN'))
    parser.add_argument('--sql', default=DEFAULT_SQL,
                        help=f'migration file (default {DEFAULT_SQL})')
    parser.add_argument('--reader-secret-arn',
                        help='Secrets Manager ARN holding the ws13_reader '
                             'password; promotes the role to LOGIN')
    parser.add_argument('--region',
                        default=os.environ.get('AWS_DEFAULT_REGION', 'us-west-2'))
    parser.add_argument('--lock-timeout', default='30s',
                        help='fail rather than queue behind a long OCR '
                             'transaction for the table rewrite (default 30s)')
    parser.add_argument('--statement-timeout', default='120s',
                        help='cap every --check query so the deploy gate '
                             'cannot hang on the database (default 120s)')
    parser.add_argument('--require-login', action='store_true',
                        help='--check also fails when ws13_reader is NOLOGIN')
    parser.add_argument('--require-provenance', action='store_true',
                        help='--check also fails unless the provenance '
                             'backfill has run: no licensed or research copy '
                             'without a rights_basis, no unknown rights '
                             'class, and source_url coverage above '
                             '--min-source-url-coverage')
    parser.add_argument('--min-source-url-coverage', type=float,
                        default=MIN_SOURCE_URL_COVERAGE,
                        help=f'fraction of ws13_documents that must carry a '
                             f'source_url under --require-provenance '
                             f'(default {MIN_SOURCE_URL_COVERAGE})')
    parser.add_argument('--plaintext-password', action='store_true',
                        help='send the ws13_reader password as a literal '
                             'instead of a SCRAM-SHA-256 verifier; the RDS '
                             'server log then captures it under '
                             "log_statement = 'ddl'")
    parser.add_argument('--no-verify-reader-login', action='store_false',
                        dest='verify_reader_login',
                        help='skip the post-install login as ws13_reader')
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    if args.reader_secret_arn and not args.apply:
        sys.exit('--reader-secret-arn writes, so it requires --apply')
    if args.require_login and not args.check:
        sys.exit('--require-login only applies to --check')
    if args.require_provenance and not args.check:
        sys.exit('--require-provenance only applies to --check')
    if not 0 < args.min_source_url_coverage <= 1:
        sys.exit('--min-source-url-coverage must be in (0, 1]')
    if args.plaintext_password and not args.reader_secret_arn:
        sys.exit('--plaintext-password only applies to --reader-secret-arn')

    with open(args.sql, encoding='utf-8') as handle:
        statements = split_statements(handle.read())
    if not statements:
        sys.exit(f'{args.sql} contains no statements')

    if args.dry_run:
        print(f'{args.sql}: {len(statements)} statements')
        for number, statement in enumerate(statements, 1):
            print(f'  [{number:2d}] {_short(statement["label"], 100)}')
        return 0

    if not args.dsn:
        sys.exit('need --dsn (or WS13_DB_DSN)')

    if args.check:
        with psycopg.connect(args.dsn, autocommit=True) as conn:
            # A preflight gate must not be able to hang the deploy: two of
            # the checks read all 56,282 ws13_documents rows, and this bounds
            # them. run_checks() reports a timeout as a MISS, not a crash.
            conn.execute('SELECT set_config(%s, %s, false)',
                         ('statement_timeout', args.statement_timeout))
            results = run_checks(
                conn, require_login=args.require_login,
                require_provenance=args.require_provenance,
                min_source_url_coverage=args.min_source_url_coverage)
        failed = [row for row in results if not row[1]]
        for name, ok, detail in results:
            print(f'  {"OK  " if ok else "MISS"} {name}'
                  + (f': {detail}' if detail else ''))
        print(f'{len(results) - len(failed)}/{len(results)} checks passed')
        return 1 if failed else 0

    conn = psycopg.connect(args.dsn, autocommit=False)
    try:
        # The generated column takes ACCESS EXCLUSIVE on ws13_documents for the
        # whole transaction. Time out rather than block the OCR fleet forever.
        # set_config() rather than `SET lock_timeout = %s`: psycopg binds
        # server-side, and SET does not accept a $1 placeholder -- that spells
        # itself as a syntax error on the first statement of the migration.
        conn.execute('SELECT set_config(%s, %s, false)',
                     ('lock_timeout', args.lock_timeout))
        print(f'applying {args.sql}: {len(statements)} statements')
        started = time.perf_counter()
        report = apply_migrations(conn, statements)
        conn.commit()
        conn.autocommit = True
        total = time.perf_counter() - started
        changed = sum(entry['rows'] for entry in report if entry['rows'] > 0)
        print(f'committed {len(report)} statements in {total:.1f}s; '
              f'{changed} rows changed')
    except Exception:
        conn.rollback()
        print('ROLLED BACK: nothing was applied', file=sys.stderr)
        raise

    if args.reader_secret_arn:
        password = reader_password(args.reader_secret_arn, args.region)
        shape = install_reader_password(
            conn, password, plaintext=args.plaintext_password)
        print(f'{READER_ROLE}: LOGIN enabled, {shape} installed from '
              f'{args.reader_secret_arn}')
        if args.verify_reader_login:
            try:
                verify_reader_login(args.dsn, password)
            except Exception as exc:                   # noqa: BLE001 - report
                conn.close()
                sys.exit(f'{READER_ROLE} password installed but the '
                         f'verification login failed: {exc}. Re-run with '
                         f'--no-verify-reader-login if this host cannot '
                         f'reach the database as {READER_ROLE}, or with '
                         f'--plaintext-password if the verifier is rejected.')
            print(f'{READER_ROLE}: verified by logging in')
    else:
        print(f'{READER_ROLE}: left NOLOGIN (no --reader-secret-arn). The '
              f'retrieval Lambda cannot connect until it is promoted.')

    results = run_checks(conn)
    failed = [row for row in results if not row[1]]
    for name, ok, detail in failed:
        print(f'  MISS {name}' + (f': {detail}' if detail else ''),
              file=sys.stderr)
    conn.close()
    return 1 if failed else 0


if __name__ == '__main__':
    raise SystemExit(main())
