#!/usr/bin/env python3
"""Offline proof that the ANN index and the retrieval query still agree.

Postgres uses an expression index only when the query repeats the expression
and the operator matches the index opclass. `titan_embedding::halfvec(1024)`
indexed with halfvec_cosine_ops is used by `... <=> %s::halfvec(1024)` and by
nothing else: cast only the column, or reach for <-> instead of <=>, and the
plan quietly turns into a sequential scan over all 852,027 chunks. Nothing
raises, nothing warns, the same rows come back -- just far too late for the
30 s API Gateway deadline in front of the retrieval Lambda. That failure is
invisible in every test that only checks the result set, so it is asserted
here instead, statically, on every deploy.

The six SQL constants are read from infra/ws13_query_lambda.py, which is the
single source of truth for them; nothing is re-typed. This module checks:

  * the expression CREATE INDEX indexes is the expression the ORDER BY sorts
    on, once the bound parameter is set aside;
  * HALFVEC_EXPR is byte-identical to what CREATE INDEX actually indexes;
  * the opclass is halfvec_cosine_ops and the operator is <=>;
  * BOTH operands carry ::halfvec(1024) -- casting only the column fails the
    same silent way, because the operator then resolves against vector;
  * EXPLAIN_SQL contains ORDER_BY_SQL verbatim and keeps a LIMIT, since an
    HNSW scan is only ever chosen for an ordered LIMIT query, and a probe
    that lost its LIMIT would fail for a reason unrelated to the contract;
  * ANALYZE targets ws13_chunks: until statistics exist for the new
    expression the planner will not cost the index at all;
  * the live SQL BUILDERS agree with the constants -- vector_ann_sql() pastes
    ORDER_BY_SQL verbatim for the FILTERED shape as well, explain_ann_sql()
    with analyze=False wraps that same statement in a PLAIN EXPLAIN, and the
    placeholder count matches the parameter list --verify binds positionally.
    The constants alone certify a statement no real request issues: every
    request carries filters, filter_sql() flattens into a semi-join, and the
    semi-join is the shape that can lose the index.

No database, no network, no credentials, so it runs unconditionally in CI and
in deploy.sh preflight. assert_plan() is the live half of the same trap, used
by pipelines/ws13_build_ann_index.py against a real EXPLAIN plan.
"""

from __future__ import annotations

import argparse
import ast
import importlib.util
import json
from pathlib import Path
import re
import sys


# Two layouts have to work and neither is hypothetical. A developer runs
# `python3 pipelines/ws13_index_contract.py` from the repository root, where
# the query Lambda is at infra/ws13_query_lambda.py; the fleet untars
# ws13/fleet/bundle.tar.gz FLAT into /opt/ws13 (infra/ws13_fleet.yaml) and
# runs `python3 ws13_build_ann_index.py` there, where parents[1] is /opt. A
# repo-only default therefore sent every on-host step looking for
# /opt/infra/ws13_query_lambda.py and aborted with exit 2 before it did
# anything -- including the two steps that never touch the database.
# pipelines/ws13_seed.py:migrations_path() resolves its own sibling the same
# way, for the same reason.
QUERY_LAMBDA_CANDIDATES = (
    Path(__file__).resolve().parent / 'ws13_query_lambda.py',
    Path(__file__).resolve().parents[1] / 'infra' / 'ws13_query_lambda.py',
)


def resolve_query_lambda(candidates=QUERY_LAMBDA_CANDIDATES):
    """The first candidate that exists; the repository path when none does.

    Falling back to the repository path rather than to None keeps the "is
    missing: it is the single source of truth" message naming a real
    location, which is the one an operator can act on.
    """
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[-1]


QUERY_LAMBDA = resolve_query_lambda()
REQUIRED = ('HALFVEC_EXPR', 'INDEX_NAME', 'CREATE_INDEX_SQL', 'ORDER_BY_SQL',
            'ANALYZE_SQL', 'EXPLAIN_SQL')
# The three callables pipelines/ws13_build_ann_index.py --verify depends on.
# The constants alone certify a statement no real request issues: every
# request carries filters, filter_sql() flattens into a semi-join, and the
# semi-join is the shape that can lose the index. So the contract also checks
# the functions that build what production runs, and it checks them here --
# statically, on every deploy -- rather than on the in-VPC host with a deploy
# already in flight.
BUILDER_NAME = 'vector_ann_sql'
EXPLAIN_BUILDER_NAME = 'explain_ann_sql'
PROBE_STRATEGY_NAME = 'plan_filtered_probe'
# Syntactically a vector literal, never executed: builder_problems() only
# reads the SQL these functions return.
BUILDER_PROBE_LITERAL = '[0.0]'

# These are the assertions, not a second copy of the SQL. The SQL itself is
# only ever read out of ws13_query_lambda.py.
EXPECTED_TABLE = 'ws13_chunks'
# Pinned golden values. The runbook, the operator's DROP INDEX escape hatch
# and every other module name the index; renaming it has to be a deliberate
# edit here too. The column is pinned because it is the only production
# vector: titan_embedding is complete (0 NULL over 852,027 rows), while
# `embedding` still has 593,649 NULLs and qwen_embedding is dead.
EXPECTED_INDEX_NAME = 'ws13_chunks_titan_hnsw'
EXPECTED_COLUMN = 'titan_embedding'
EXPECTED_OPCLASS = 'halfvec_cosine_ops'
EXPECTED_OPERATOR = '<=>'
EXPECTED_DIMS = 1024
EXPECTED_ACCESS_METHOD = 'hnsw'
# halfvec_cosine_ops answers <=> only. <-> (L2) and <#> (inner product) parse
# and run against the same column and are the two ways to lose the index.
WRONG_OPERATORS = ('<->', '<#>')
CAST_RE = re.compile(r'^(?P<operand>.+?)::\s*halfvec\s*\(\s*(?P<dims>\d+)\s*\)'
                     r'\s*(?:ASC)?$', re.I | re.S)
# One leading `alias.` qualifier resolves to the same column, so the planner
# matches the same expression tree. It is the only difference tolerated
# between the indexed expression and the ORDER BY operand, alongside
# whitespace, which the SQL parser ignores outright.
ALIAS_RE = re.compile(r'^[a-z_][a-z0-9_$]*\.(?=[a-z_])', re.I)

_CACHE: dict[str, tuple[dict, str]] = {}
_MODULES: dict[str, object] = {}


class ContractError(RuntimeError):
    """A contract violation the caller must not continue past."""


def _paren_span(text, start):
    """Content of the parenthesis group opening at `start`, and its end index.

    A regex cannot do this: the indexed expression contains its own
    parentheses (the halfvec dimension), so the nesting has to be counted.
    """
    if start >= len(text) or text[start] != '(':
        raise ContractError(f'expected "(" at offset {start} in: {text!r}')
    depth = 0
    for i in range(start, len(text)):
        if text[i] == '(':
            depth += 1
        elif text[i] == ')':
            depth -= 1
            if depth == 0:
                return text[start + 1:i], i
    raise ContractError(f'unbalanced parentheses in: {text!r}')


def _literal(node, known):
    """Evaluate a module-level string constant without executing the module.

    Handles the shapes a hand-written constant actually takes: a literal, an
    implicit or explicit concatenation, a reference to an earlier constant,
    and an f-string interpolating one (which is how CREATE_INDEX_SQL is likely
    to be built out of INDEX_NAME and HALFVEC_EXPR).
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name) and node.id in known:
        return known[node.id]
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _literal(node.left, known) + _literal(node.right, known)
    if isinstance(node, ast.JoinedStr):
        out = []
        for part in node.values:
            if isinstance(part, ast.Constant) and isinstance(part.value, str):
                out.append(part.value)
            elif (isinstance(part, ast.FormattedValue)
                  and isinstance(part.value, ast.Name)
                  and part.format_spec is None and part.conversion in (-1, None)
                  and part.value.id in known):
                out.append(known[part.value.id])
            else:
                raise ContractError('f-string interpolates something this '
                                    'reader cannot resolve offline')
        return ''.join(out)
    raise ContractError(f'not a static string constant: {ast.dump(node)[:120]}')


def _from_source(path):
    """Read the constants straight out of the file with ast."""
    tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
    known: dict[str, str] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name):
            continue
        try:
            known[target.id] = _literal(node.value, known)
        except ContractError:
            continue
    missing = [name for name in REQUIRED if name not in known]
    if missing:
        raise ContractError(
            f'{path} does not export {", ".join(missing)} as a static string; '
            'install the module dependencies so it can be imported instead')
    return {name: known[name] for name in REQUIRED}


def _missing_message(path):
    """Name every place that was searched, not just the one that lost.

    On the flat /opt/ws13 host the interesting fact is which two paths were
    tried, because the fix is to put the file in the bundle -- a message that
    names only the repository layout sends the operator to a directory that
    does not exist there.
    """
    message = (f'{path} is missing: it is the single source of truth for the '
               'halfvec index contract')
    if Path(path).resolve() == QUERY_LAMBDA.resolve():
        searched = ', '.join(str(candidate)
                             for candidate in QUERY_LAMBDA_CANDIDATES)
        message += f' (searched: {searched})'
    return message


def _import_from_path(path):
    """Execute the query Lambda from an exact path and hand back the module.

    Never by name. import_module would hand back whatever ws13_query_lambda
    is already in sys.modules or first on sys.path, which is how a checker
    ends up passing against a file it never read. The directory still goes on
    sys.path so the module's own sibling imports resolve.
    """
    sys.path.insert(0, str(path.parent))
    try:
        unique = '_ws13_contract_' + re.sub(r'\W', '_', str(path))
        spec = importlib.util.spec_from_file_location(unique, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        if sys.path and sys.path[0] == str(path.parent):
            sys.path.pop(0)


def query_module(path=QUERY_LAMBDA):
    """The imported query Lambda, memoised by path, or a ContractError.

    load_constants() may fall back to reading the file with ast, but a caller
    that needs the SQL BUILDERS needs the real module: the filtered probe is
    the shape production actually issues, and re-typing it in the verifier
    would be a second source of truth, which is the one thing this module
    exists to prevent.
    """
    path = Path(path).resolve()
    key = str(path)
    if key in _MODULES:
        return _MODULES[key]
    if not path.exists():
        raise ContractError(_missing_message(path))
    try:
        module = _import_from_path(path)
    except Exception as exc:
        raise ContractError(f'{path} cannot be imported, so {BUILDER_NAME}() '
                            f'is unavailable and the filtered plan cannot be '
                            f'asserted: {type(exc).__name__}: {exc}') from exc
    _MODULES[key] = module
    return module


def load_constants(path=QUERY_LAMBDA):
    """(values, source) for the six contract constants.

    The import is tried first so any construction the module chooses works.
    It is allowed to fail: a Lambda handler may build boto3 clients or read
    env at import time, and this check has to run in CI with no credentials
    and no third-party packages. The fallback parses the same file, so both
    paths read one source of truth and neither re-types a string.
    """
    path = Path(path).resolve()
    if not path.exists():
        raise ContractError(_missing_message(path))
    try:
        module = _import_from_path(path)
        values = {key: getattr(module, key) for key in REQUIRED}
        if all(isinstance(value, str) for value in values.values()):
            _MODULES.setdefault(str(path), module)
            return values, 'import'
    except Exception:
        pass
    return _from_source(path), 'ast'


def constants(path=QUERY_LAMBDA):
    """load_constants() memoised by path; values only."""
    key = str(Path(path))
    if key not in _CACHE:
        _CACHE[key] = load_constants(path)
    return _CACHE[key][0]


def _collapse(text):
    return ' '.join(text.split())


def _strip_alias(expr):
    return ALIAS_RE.sub('', expr.strip(), count=1)


def index_expression(create_sql):
    """(expression, opclass, index_name, table) parsed out of CREATE INDEX."""
    head = re.search(
        r'CREATE\s+(?:UNIQUE\s+)?INDEX\s+(?:CONCURRENTLY\s+)?'
        r'(?:IF\s+NOT\s+EXISTS\s+)?(?P<name>[a-z_][a-z0-9_$]*)\s+'
        r'ON\s+(?:ONLY\s+)?(?P<table>[a-z_][a-z0-9_$.]*)', create_sql, re.I)
    if not head:
        raise ContractError(f'cannot parse CREATE INDEX: {create_sql!r}')
    using = re.search(r'USING\s+(?P<am>[a-z_]+)\s*(?=\()', create_sql, re.I)
    if not using:
        raise ContractError('CREATE INDEX names no access method')
    elements, _ = _paren_span(create_sql, create_sql.index('(', using.end()))
    elements = elements.strip()
    expr, end = _paren_span(elements, 0)
    opclass = elements[end + 1:].strip()
    return {'expression': expr, 'opclass': opclass, 'name': head.group('name'),
            'table': head.group('table'), 'access_method': using.group('am')}


def order_by_operands(order_by_sql):
    """(left, right) around the distance operator in ORDER BY."""
    body = re.match(r'\s*ORDER\s+BY\s+(?P<body>.*)$', order_by_sql, re.I | re.S)
    if not body:
        raise ContractError(f'ORDER_BY_SQL does not start with ORDER BY: '
                            f'{order_by_sql!r}')
    parts = body.group('body').split(EXPECTED_OPERATOR)
    if len(parts) != 2:
        raise ContractError(f'expected exactly one {EXPECTED_OPERATOR} in '
                            f'{order_by_sql!r}, found {len(parts) - 1}')
    return parts[0].strip(), parts[1].strip()


def check(path=QUERY_LAMBDA):
    """Every way the index and the query could disagree, as a list of strings."""
    problems: list[str] = []
    try:
        values, source = load_constants(path)
    except ContractError as exc:
        return [str(exc)]

    for name in REQUIRED:
        value = values.get(name)
        if not isinstance(value, str) or not value.strip():
            problems.append(f'{name} is not a non-empty string')
    if problems:
        return problems

    create_sql = values['CREATE_INDEX_SQL']
    order_sql = values['ORDER_BY_SQL']
    explain_sql = values['EXPLAIN_SQL']
    analyze_sql = values['ANALYZE_SQL']

    try:
        index = index_expression(create_sql)
        left, right = order_by_operands(order_sql)
    except ContractError as exc:
        return problems + [str(exc)]

    if values['INDEX_NAME'] != EXPECTED_INDEX_NAME:
        problems.append(f'INDEX_NAME is {values["INDEX_NAME"]!r}, not the '
                        f'contract name {EXPECTED_INDEX_NAME!r}')
    if index['name'] != values['INDEX_NAME']:
        problems.append(f'CREATE_INDEX_SQL builds {index["name"]!r} but '
                        f'INDEX_NAME is {values["INDEX_NAME"]!r}')
    if index['table'].split('.')[-1] != EXPECTED_TABLE:
        problems.append(f'CREATE_INDEX_SQL indexes {index["table"]!r}, '
                        f'not {EXPECTED_TABLE}')
    if index['access_method'].lower() != EXPECTED_ACCESS_METHOD:
        problems.append(f'access method is {index["access_method"]!r}, '
                        f'not {EXPECTED_ACCESS_METHOD}')
    if index['opclass'] != EXPECTED_OPCLASS:
        problems.append(f'opclass is {index["opclass"]!r}, not '
                        f'{EXPECTED_OPCLASS} -- only that opclass answers '
                        f'{EXPECTED_OPERATOR}')
    if index['expression'] != values['HALFVEC_EXPR']:
        problems.append(f'HALFVEC_EXPR {values["HALFVEC_EXPR"]!r} is not what '
                        f'CREATE_INDEX_SQL indexes ({index["expression"]!r})')

    indexed_column = _strip_alias(index['expression']).split('::')[0].strip()
    if indexed_column != EXPECTED_COLUMN:
        problems.append(f'the index is built on {indexed_column!r}, not '
                        f'{EXPECTED_COLUMN!r}, which is the only complete '
                        'production vector')

    for wrong in WRONG_OPERATORS:
        if wrong in order_sql:
            problems.append(f'ORDER_BY_SQL uses {wrong}, which {EXPECTED_OPCLASS} '
                            f'does not answer: the index would be ignored')

    # Both operands, not just the column. Casting one side leaves the operator
    # resolving against vector, which the halfvec index cannot serve.
    dims = []
    for label, operand in (('indexed expression', index['expression']),
                           ('ORDER BY left operand', left),
                           ('ORDER BY bound parameter', right)):
        cast = CAST_RE.match(operand.strip())
        if not cast:
            problems.append(f'{label} is not cast to halfvec: {operand.strip()!r}')
            continue
        dims.append(int(cast.group('dims')))
        if label == 'ORDER BY bound parameter':
            placeholder = cast.group('operand').strip()
            if placeholder != '%s':
                problems.append(
                    f'ORDER BY parameter is {placeholder!r}, not %s; '
                    'pipelines/ws13_build_ann_index.py binds positionally')
    if dims and len(set(dims)) != 1:
        problems.append(f'halfvec dimensions disagree across the operands: {dims}')
    if dims and dims[0] != EXPECTED_DIMS:
        problems.append(f'halfvec({dims[0]}) does not match titan_embedding '
                        f'vector({EXPECTED_DIMS})')

    # The parameter placeholder is set aside above; here the two expressions
    # have to be the same one, allowing only the alias qualifier and
    # whitespace, both of which the parser resolves away.
    if _collapse(_strip_alias(index['expression'])) != _collapse(_strip_alias(left)):
        problems.append(
            f'the indexed expression {index["expression"]!r} and the ORDER BY '
            f'expression {left!r} are not the same expression; an expression '
            'index is only used when the query repeats it')

    if not re.match(r'\s*EXPLAIN\b', explain_sql, re.I):
        problems.append('EXPLAIN_SQL is not an EXPLAIN')
    options = re.match(r'\s*EXPLAIN\s*(?:\((?P<opts>[^)]*)\))?', explain_sql, re.I)
    opts = (options.group('opts') or '') if options else ''
    # ANALYZE is deliberately NOT required here any more. EXPLAIN (ANALYZE)
    # RUNS the statement, and pipelines/ws13_build_ann_index.py --verify is a
    # deploy gate: on a database where ws13_chunks_titan_hnsw has not been
    # built yet -- the normal state before Phase A -- requiring ANALYZE means
    # the gate answers "is the index used?" by executing the 852,027-row
    # sequential scan it exists to prevent, against production RDS, with no
    # bound. The gate now checks pg_class first and issues a plain EXPLAIN
    # over the same statement; ANALYZE belongs to the operator's deliberate
    # --measure run, which only happens after the index is confirmed present.
    if re.search(r'\bANALYZE\b', opts, re.I) and not re.search(r'\bBUFFERS\b',
                                                               opts, re.I):
        problems.append('EXPLAIN_SQL requests ANALYZE without BUFFERS: if a '
                        'probe is going to run the statement it should report '
                        'the buffer counts, which are how a sequential scan '
                        'over 852,027 rows announces itself even when the '
                        'node label is ambiguous')
    fmt = re.search(r'FORMAT\s+(\w+)', opts, re.I)
    if fmt and fmt.group(1).upper() != 'TEXT':
        problems.append(f'EXPLAIN_SQL asks for FORMAT {fmt.group(1)}; '
                        'assert_plan() matches the text plan')
    if order_sql not in explain_sql:
        problems.append('EXPLAIN_SQL does not contain ORDER_BY_SQL verbatim, so '
                        'the probe would prove a plan the query never runs')
    if not re.search(r'\bLIMIT\b', explain_sql, re.I):
        problems.append('EXPLAIN_SQL has no LIMIT: an HNSW scan is only chosen '
                        'for an ordered LIMIT query, so the probe would fail '
                        'for a reason that is not the contract')
    holes = explain_sql.count('%s')
    if holes not in (1, 2):
        problems.append(f'EXPLAIN_SQL takes {holes} positional parameters; '
                        'ws13_build_ann_index.py binds the probe vector and at '
                        'most a limit')

    if not re.match(r'\s*ANALYZE\s+(?:VERBOSE\s+)?' + EXPECTED_TABLE + r'\b',
                    analyze_sql, re.I):
        problems.append(f'ANALYZE_SQL does not analyze {EXPECTED_TABLE}; without '
                        'statistics for the new expression the planner will not '
                        'cost the index')
    # The builders are functions, so they can only be checked when this
    # environment can EXECUTE the module. source == 'ast' means it cannot (a
    # Lambda handler is allowed to need packages CI does not have), and the
    # static strings above are then the whole offline check. Nothing is
    # silently certified by that: pipelines/ws13_build_ann_index.py --verify
    # calls query_module() itself and refuses with exit 2 rather than pass a
    # gate whose filtered statement it could not build.
    if source == 'import':
        problems.extend(builder_problems(path, values))
    return problems


def builder_problems(path=QUERY_LAMBDA, values=None):
    """What the live SQL BUILDER gets wrong, as a list of strings.

    The checks above assert the constants; this asserts the function that
    pastes them. The unfiltered probe proves nothing about the shape
    production issues: every real request carries filters, the document
    predicates become a semi-join in the WHERE, and a semi-join is precisely
    what pushes the planner off an HNSW index and onto a sequential scan of
    all 852,027 chunks. pipelines/ws13_build_ann_index.py --verify EXPLAINs
    exactly what this function returns, so its dependency on the name, the
    signature and the placeholder count is asserted here rather than
    discovered on the in-VPC host with the deploy already running.
    """
    if values is None:
        try:
            values, _source = load_constants(path)
        except ContractError as exc:
            return [str(exc)]
    try:
        module = query_module(path)
    except ContractError as exc:
        return [str(exc)]
    problems = []
    missing = [name for name in (BUILDER_NAME, EXPLAIN_BUILDER_NAME,
                                 PROBE_STRATEGY_NAME)
               if not callable(getattr(module, name, None))]
    if missing:
        return [f'{Path(path).name} exports no callable {", ".join(missing)}; '
                'pipelines/ws13_build_ann_index.py --verify calls those to '
                'EXPLAIN the statement each filter set really produces']
    builder = getattr(module, BUILDER_NAME)
    explainer = getattr(module, EXPLAIN_BUILDER_NAME)

    for label, filters in (('{}', {}),
                           ("{'state','county'}", {'state': 'ID',
                                                   'county': 'Cassia'})):
        try:
            sql, params = builder(dict(filters), BUILDER_PROBE_LITERAL, 1)
            params = list(params)
        except Exception as exc:
            problems.append(f'{BUILDER_NAME}({label}, literal, over_fetch) '
                            f'raised {type(exc).__name__}: {exc}; --verify '
                            'calls it with exactly that signature')
            continue
        if values['ORDER_BY_SQL'] not in sql:
            problems.append(f'{BUILDER_NAME}({label}) does not paste '
                            'ORDER_BY_SQL verbatim: an expression index is '
                            'only used when the query repeats the expression')
        if not re.search(r'\bLIMIT\b', sql, re.I):
            problems.append(f'{BUILDER_NAME}({label}) has no LIMIT: an HNSW '
                            'scan is only ever chosen for an ordered LIMIT '
                            'query')
        if sql.count('%s') != len(params):
            problems.append(f'{BUILDER_NAME}({label}) returns '
                            f'{sql.count("%s")} placeholders for '
                            f'{len(params)} parameters; --verify binds them '
                            'positionally into EXPLAIN')
        head = re.split(r'\bORDER\s+BY\b', sql, maxsplit=1, flags=re.I)[0]
        if filters and 'JOIN' in head.upper():
            problems.append(f'{BUILDER_NAME}({label}) joins before the ORDER '
                            'BY: pgvector drives the HNSW index only when the '
                            'ordering expression references one relation, so '
                            'document predicates belong inside one EXISTS')
        try:
            gate_sql, gate_params = explainer(dict(filters),
                                              BUILDER_PROBE_LITERAL, 1,
                                              analyze=False)
        except Exception as exc:
            problems.append(f'{EXPLAIN_BUILDER_NAME}({label}, literal, '
                            f'over_fetch, analyze=False) raised '
                            f'{type(exc).__name__}: {exc}; that is the call '
                            '--verify makes for every filter shape')
            continue
        # analyze=False has to mean a PLAN, not a run. EXPLAIN (ANALYZE)
        # executes the statement, so a gate built on it answers "is the index
        # used?" by performing the 852,027-row sequential scan whose absence
        # it is checking for.
        if re.search(r'\bANALYZE\b', gate_sql.split('SELECT')[0], re.I):
            problems.append(f'{EXPLAIN_BUILDER_NAME}({label}, analyze=False) '
                            'still requests ANALYZE, which EXECUTES the '
                            'statement; the deploy gate must cost the plan, '
                            'not run the scan it is looking for')
        if sql not in gate_sql:
            problems.append(f'{EXPLAIN_BUILDER_NAME}({label}) does not wrap '
                            f'the statement {BUILDER_NAME}({label}) builds, '
                            'so the gate would certify a plan the query never '
                            'runs')
        if list(gate_params) != params:
            problems.append(f'{EXPLAIN_BUILDER_NAME}({label}) binds different '
                            f'parameters than {BUILDER_NAME}({label}); the '
                            'planner costs the values it is given')
    return problems


def plan_problems(plan_text, index_name=None, allow_seq_scan_on=(),
                  require_index=True):
    """Everything wrong with a live EXPLAIN plan, as a list of strings.

    allow_seq_scan_on names the relations whose sequential scan is not the
    failure this gate is about. The filtered probe puts every document
    predicate inside one EXISTS against ws13_documents, and a scan of those
    56,282 rows is a legitimate plan; a scan of the 852,027-row chunk table
    is the failure, whatever else the plan does.

    require_index is False for the one shape where another plan is the RIGHT
    answer: when the caller has already resolved the document filter to a
    bounded sha256 set the probe becomes `c.sha256 = ANY(%s)`, and
    ws13_chunks_sha then gives an exact result with no ANN truncation at all.
    Demanding the HNSW index there would fail a plan that is better than the
    one being demanded. What is never acceptable is the sequential scan.
    """
    if index_name is None:
        index_name = constants()['INDEX_NAME']
    problems = []
    if not plan_text or not plan_text.strip():
        return ['EXPLAIN returned no plan text']
    if require_index and not re.search(
            r'Index (?:Only )?Scan using ' + re.escape(index_name), plan_text):
        problems.append(f'plan does not use {index_name}')
    allowed = {name.lower() for name in allow_seq_scan_on}
    for scan in re.finditer(r'(?:Parallel\s+)?Seq Scan(?:\s+on\s+(?P<rel>\S+))?',
                            plan_text):
        if (scan.group('rel') or '').lower() in allowed:
            continue
        problems.append(f'plan contains "{scan.group(0).strip()}": the probe '
                        'reads ws13_chunks end to end instead of the ANN index')
    return problems


def assert_plan(plan_text, index_name=None, allow_seq_scan_on=(),
                require_index=True):
    """Raise unless the live plan uses the HNSW index. Used by Phase A step c."""
    problems = plan_problems(plan_text, index_name, allow_seq_scan_on,
                             require_index)
    if problems:
        raise ContractError('; '.join(problems) + '\n--- plan ---\n'
                            + plan_text.strip()[:4000])


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--query-lambda', default=str(QUERY_LAMBDA),
                        help='module exporting the contract constants')
    parser.add_argument('--json', action='store_true',
                        help='machine-readable result')
    parser.add_argument('--show', action='store_true',
                        help='print the resolved constants (they are not secret)')
    args = parser.parse_args(argv)

    path = Path(args.query_lambda)
    problems = check(path)
    resolved, source = ({}, 'unavailable')
    try:
        resolved, source = load_constants(path)
    except ContractError:
        pass

    if args.json:
        print(json.dumps({'source': source, 'problems': problems,
                          'constants': resolved if args.show else {}}, indent=2))
    else:
        if args.show:
            for name in REQUIRED:
                print(f'{name} = {resolved.get(name)!r}')
        if problems:
            print(f'ws13 index contract FAILED ({len(problems)} problem(s), '
                  f'constants read via {source}):')
            for problem in problems:
                print(f'  - {problem}')
        else:
            print(f'ws13 index contract ok (constants read via {source}): '
                  f'{resolved.get("INDEX_NAME")} on '
                  f'{resolved.get("HALFVEC_EXPR")} using {EXPECTED_OPCLASS} '
                  f'{EXPECTED_OPERATOR}')
    return 1 if problems else 0


if __name__ == '__main__':
    sys.exit(main())
