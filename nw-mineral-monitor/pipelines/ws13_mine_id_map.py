#!/usr/bin/env python3
"""Build ws13_mine_id_map: the missing identifier bridge, front end -> corpus.

The content bridge already works; the identifier bridge does not exist. The
map front end addresses mines with ids like 'stategeo-igs-dd-1-if0126' --
tools/test_doc_viewer.js:25 and pipelines/config/ws12_documents.json both use
that spelling, and it is exactly stateSurveySafeId() (site/index.html:1219)
applied to the record ids enumerated in build-inputs/data/sites/*.json.
ws13_documents.mine_ids holds a completely different namespace: AZGS
collection ids ('ADMM-...', 13,036 rows) and bare IGS survey codes ('SP0145',
'WL0383', 'BA0024'), because mine_file_harvest.py seeds igs_mines from the raw
IGSID attribute (_seed_igs validates [A-Z]{2}\\d{4}). The two namespaces do not
intersect: mine_ids ILIKE 'stategeo%' over all 56,282 documents returns ZERO
rows, so every citation lookup keyed by a front-end id comes back empty and
reads to a user as "this mine has no documents" rather than "this id was never
translated".

The documents themselves are present and correct. IF0126's three acceptance
documents are in the corpus exactly as MINE-FILE-HARVEST.md records them:
MRDS-W015681 and MILS-160230014 under ws12/originals/, plus IF0131_001.pdf
under ws12/research-copies/. Only the identifier is missing.

This writes one row per front-end id, deriving the corpus id in strict
precedence order and recording the method, a confidence and the evidence on
every row, so that a low-confidence guess can never be read as a verified
mapping:

  embedded_code     the last code-shaped segment of the front-end id is
                    itself a corpus mine_id (1.0, verified) or appears in a
                    document's s3_key / source_url (0.9, unverified)
  prefix_namespace  the front-end namespace prefix is translated onto the
                    corpus namespace ('azgs-...' -> 'ADMM-...') and the
                    rebuilt id is a corpus mine_id (0.95, verified)
  fuzzy_name        difflib over ws13_documents.mine_names, blocked by state,
                    confidence capped at 0.6 and never verified
  unmapped          ws13_mine_id NULL, with every attempt recorded

The path probes behind the two 'in a document's path' tiers match a whole path
component ('.../ADMM-1552428849304-690/report.pdf') or a token that carries a
letter; a code that is only digits is never probed against a path at all,
because a bare numeric run found somewhere in a URL is a coincidence as often
as an id and the mrds_*.json files alone declare 124,598 eight-digit dep_ids.
Those ids still resolve at 1.0 against mine_ids, where an equality against a
stored id validates itself.

Nothing is dropped and nothing is guessed. An id whose evidence is ambiguous
-- a name matching several corpus mine ids, a name recorded for several states
('copper king' is in AZ, ID and MT), two names tied at the same difflib ratio,
a path token shared by several -- lands in 'unmapped' with the candidates in
its evidence rather than having one picked for it. That matters more now than
when this was written as a reporting aid: the retrieval Lambda resolves an
incoming filters.mine_id through this table before it applies the mine
predicate, so a wrong high-confidence row does not read as a gap, it silently
serves one mine's documents under another mine's name.

Ids the front end enumerates but the corpus has never seen (most of the
179,404 site records: the corpus is dominated by Arizona and Idaho) are
written as 'unmapped' rows so that coverage is a measurement rather than an
absence.

Every mapped row carries ws13_mine_id_all as well as ws13_mine_id: every
stored spelling of the corpus id, because mine_file_harvest.py seeds the id
from a raw survey attribute and does not case-fold it, so 'SP0145' and
'sp0145' are two different values to ws13_documents_mines, the GIN index the
retrieval path probes with `mine_ids @> ARRAY[...]`. A reader must match
against ws13_mine_id_all (`mine_ids && ws13_mine_id_all`) or it silently loses
the documents filed under the other spelling. ws13_mine_id is the spelling
most documents use, and is always one of the members.

Reruns are safe: ON CONFLICT (front_end_id) DO UPDATE, the update is refused
when it would replace a verified=true row with an unverified one, and a rerun
that derives the same answer writes zero rows and leaves updated_at as the
real "last changed" time.
"""
from __future__ import annotations

import argparse
import collections
import dataclasses
import datetime as dt
import difflib
import glob
import heapq
import json
import os
import re
import sys

import psycopg
from psycopg.types.json import Jsonb

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SITE_ID_DIR = os.path.join(REPO_ROOT, 'build-inputs', 'data', 'sites')
DOCS_INDEX = os.path.join(REPO_ROOT, 'site', 'data', 'docs', 'index.json')
DOC_REGISTRY = os.path.join(REPO_ROOT, 'pipelines', 'config',
                            'ws12_documents.json')

METHODS = ('embedded_code', 'prefix_namespace', 'fuzzy_name', 'unmapped')
# Confidence tiers. The two exact tiers are separated from the fuzzy tier by a
# wide gap on purpose: an operator filtering `confidence >= 0.8` must never
# pick up a difflib guess, whatever ratio it scored.
CONF_CODE_IN_MINE_IDS = 1.0
CONF_CODE_IN_PATH = 0.9
CONF_PREFIX_IN_MINE_IDS = 0.95
CONF_PREFIX_IN_PATH = 0.85
FUZZY_CEILING = 0.6
# Applied when the front-end record has no state, so a name match that could
# not be state-blocked is visibly weaker than one that could.
UNKNOWN_STATE_FACTOR = 0.9
FUZZY_CANDIDATES = 30
# A trigram shared by more than this many corpus names carries no signal, and
# only the most selective grams of a query are scanned at all. Together these
# bound the candidate sweep so one very common bigram cannot dominate it.
TRIGRAM_POSTING_CAP = 5000
TRIGRAM_PROBES = 12
CONFIDENCE_BANDS = (
    ('exact_1.00', 1.0),
    ('strong_0.90_0.99', 0.90),
    ('good_0.80_0.89', 0.80),
    ('fuzzy_0.50_0.79', 0.50),
    ('weak_below_0.50', 1e-9),
    ('unmapped_0.00', 0.0),
)

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS ws13_mine_id_map (
  front_end_id TEXT PRIMARY KEY,
  ws13_mine_id TEXT,
  ws13_mine_id_all TEXT[],
  method TEXT NOT NULL,
  confidence REAL NOT NULL,
  verified BOOLEAN NOT NULL,
  evidence JSONB NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL,
  CONSTRAINT ws13_mine_id_map_method CHECK (
    method IN ('embedded_code', 'prefix_namespace', 'fuzzy_name', 'unmapped')),
  CONSTRAINT ws13_mine_id_map_unmapped CHECK (
    (method = 'unmapped') = (ws13_mine_id IS NULL)),
  CONSTRAINT ws13_mine_id_map_spellings CHECK (
    (ws13_mine_id IS NULL) = (ws13_mine_id_all IS NULL)
    AND (ws13_mine_id IS NULL OR ws13_mine_id = ANY (ws13_mine_id_all))),
  CONSTRAINT ws13_mine_id_map_fuzzy_unverified CHECK (
    NOT (method = 'fuzzy_name' AND verified)));
CREATE INDEX IF NOT EXISTS ws13_mine_id_map_ws13
  ON ws13_mine_id_map (ws13_mine_id);
CREATE INDEX IF NOT EXISTS ws13_mine_id_map_ws13_all
  ON ws13_mine_id_map USING GIN (ws13_mine_id_all);
"""

# The retrieval Lambda reads this table as ws13_reader, SELECT only, to
# translate an incoming filters.mine_id before it builds the mine predicate.
# The query the shape above is for:
#
#   SELECT front_end_id, ws13_mine_id, ws13_mine_id_all
#     FROM ws13_mine_id_map
#    WHERE front_end_id = ANY(%s)
#      AND ws13_mine_id IS NOT NULL
#      AND (verified OR confidence >= 0.8)
#
# Neither filter is optional. Unmapped rows are in the table on purpose --
# coverage is a measurement, not an absence -- so a row is not a mapping just
# because it exists, and the fuzzy tier is a difflib guess capped at 0.6 that
# must never silently scope a search. The resolved value is the whole
# ws13_mine_id_all array, not ws13_mine_id alone: mine_ids is case-sensitive
# and the GIN index over it is exact-match.
#
# What a table this script is willing to write through looks like: name, the
# format_type() spelling of the type, and whether it is NOT NULL. Checked
# against the live catalog rather than assumed, because CREATE TABLE IF NOT
# EXISTS is silent about a table that already exists with a different
# definition -- the same hazard ws13_migrations.sql adds its DO $guard$ block
# for.
EXPECTED_COLUMNS = (
    ('front_end_id', 'text', True),
    ('ws13_mine_id', 'text', False),
    ('ws13_mine_id_all', 'text[]', False),
    ('method', 'text', True),
    ('confidence', 'real', True),
    ('verified', 'boolean', True),
    ('evidence', 'jsonb', True),
    ('updated_at', 'timestamp with time zone', True),
)
# Without these a fuzzy guess is representable as verified and an unmapped row
# is representable with a mine id, which is precisely what the tiers exist to
# prevent.
EXPECTED_CONSTRAINTS = (
    'ws13_mine_id_map_method',
    'ws13_mine_id_map_unmapped',
    'ws13_mine_id_map_spellings',
    'ws13_mine_id_map_fuzzy_unverified',
)
EXPECTED_PRIMARY_KEY = 'PRIMARY KEY (front_end_id)'

# The first guard clause is the whole point of the ON CONFLICT: a rerun after
# the corpus has changed must never replace a verified=true row with an
# unverified one. The second makes an unchanged rerun a true no-op, so
# updated_at keeps meaning "when this mapping last changed" rather than "when
# the job last ran".
UPSERT_SQL = """
INSERT INTO ws13_mine_id_map (front_end_id, ws13_mine_id, ws13_mine_id_all,
                              method, confidence, verified, evidence,
                              updated_at)
     VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (front_end_id) DO UPDATE
        SET ws13_mine_id = EXCLUDED.ws13_mine_id,
            ws13_mine_id_all = EXCLUDED.ws13_mine_id_all,
            method = EXCLUDED.method,
            confidence = EXCLUDED.confidence,
            verified = EXCLUDED.verified,
            evidence = EXCLUDED.evidence,
            updated_at = EXCLUDED.updated_at
      WHERE (ws13_mine_id_map.verified IS NOT TRUE OR EXCLUDED.verified)
        AND (ws13_mine_id_map.ws13_mine_id IS DISTINCT FROM
                 EXCLUDED.ws13_mine_id
          OR ws13_mine_id_map.ws13_mine_id_all IS DISTINCT FROM
                 EXCLUDED.ws13_mine_id_all
          OR ws13_mine_id_map.method IS DISTINCT FROM EXCLUDED.method
          OR ws13_mine_id_map.confidence IS DISTINCT FROM EXCLUDED.confidence
          OR ws13_mine_id_map.verified IS DISTINCT FROM EXCLUDED.verified
          OR ws13_mine_id_map.evidence IS DISTINCT FROM EXCLUDED.evidence)
"""

SLUG_STRIP = re.compile(r'[^a-z0-9-]+')
SLUG_EDGE = re.compile(r'^-|-$')
TOKEN_SPLIT = re.compile(r'[^A-Za-z0-9]+')
# Path components, for the whole-segment probe: URL and key separators only,
# so the punctuation inside a component ('ADMM-1552428849304-690') survives.
SEGMENT_SPLIT = re.compile(r'[/\\?#&=\s]+')
CODE_PUNCT = re.compile(r'[-_]')
WHITESPACE = re.compile(r'\s')
PAREN = re.compile(r'\([^)]*\)')
NON_ALNUM = re.compile(r'[^a-z0-9]+')
# Documents kept per evidence slot. Small on purpose: the field is a pointer
# for an operator, not a document list.
EXAMPLE_DOCUMENTS = 5


@dataclasses.dataclass(frozen=True)
class NamespaceRule:
    """One reviewed translation from a front-end prefix to a corpus id."""

    prefix: str
    code_pattern: str | None
    template: str
    note: str


# Only namespaces whose corpus spelling has actually been observed are listed.
# A prefix with no reviewed rule falls through to fuzzy_name and then to
# 'unmapped'; inventing a rule here is how a whole namespace would get mapped
# wrong at scale. Longer prefixes come first so 'azgs-admm-' wins over 'azgs-'.
NAMESPACE_RULES = (
    NamespaceRule(
        'stategeo-igs-dd-1-', r'^[A-Z]{2}\d{4}$', '{code}',
        'IGS DD-1 record; WS12 stores the bare IGSID, which '
        'mine_file_harvest.py _seed_igs validates as two letters plus four '
        'digits'),
    NamespaceRule(
        'azgs-admm-', None, 'ADMM-{code}',
        'AZGS ADMMR collection_group=ADMM collection id'),
    NamespaceRule(
        'admmr-', None, 'ADMM-{code}',
        'AZGS ADMMR collection_group=ADMM collection id'),
    NamespaceRule(
        'azgs-', None, 'ADMM-{code}',
        'AZGS ADMMR collection_group=ADMM collection id'),
    NamespaceRule(
        'mrds-', r'^\d{5,}$', '{code}',
        'USGS MRDS dep_id; the corpus records the bare numeric id'),
)


@dataclasses.dataclass(frozen=True)
class FrontEndRecord:
    """One addressable id as the front end spells it."""

    front_end_id: str
    source: str
    namespace: str | None
    record_id: str | None
    name: str | None
    state: str | None
    id_form: str


@dataclasses.dataclass(frozen=True)
class NameEntry:
    """One distinct (normalised mine name, state) seen in the corpus."""

    norm: str
    raw: str
    state: str | None
    mine_ids: tuple[str, ...]
    documents: tuple[str, ...]
    doc_count: int


@dataclasses.dataclass(frozen=True)
class FuzzyMiss:
    """Why the fuzzy tier declined, and what it declined between.

    `candidates` is populated only for the ambiguous refusals -- one normalised
    name recorded for several states, or several names tied at the same ratio.
    Those are the cases where an answer exists and picking one of them would be
    a coin flip, so the candidates go into the row's evidence.
    """

    reason: str
    candidates: tuple = ()


@dataclasses.dataclass
class Mapping:
    ws13_mine_id: str | None
    # Every stored spelling of ws13_mine_id, NULL exactly when it is NULL.
    # ws13_documents.mine_ids is case-sensitive and the GIN index over it is
    # exact-match, so a consumer that matches only ws13_mine_id loses the rows
    # filed under a different spelling of the same id.
    ws13_mine_id_all: list | None
    method: str
    confidence: float
    verified: bool
    evidence: dict


def front_end_slug(value) -> str:
    """Mirror stateSurveySafeId() in site/index.html:1219.

    The JS is .toLowerCase().replace(/[^a-z0-9-]+/g,'-').replace(/^-|-$/g,'').
    The trailing replace strips at most one leading and one trailing hyphen in
    both languages, so 'IGS DD-1 IF0126' -> 'igs-dd-1-if0126' identically.
    """
    return SLUG_EDGE.sub('', SLUG_STRIP.sub('-', str(value).lower()))


def normalize_name(value) -> str:
    """Fold a mine name for comparison: drop parentheticals and punctuation."""
    text = PAREN.sub(' ', str(value or '').lower())
    return NON_ALNUM.sub(' ', text).strip()


def looks_like_code(token: str) -> bool:
    """Is this token shaped like a survey record code rather than a word?

    Requires a digit, so 'MRDS' and 'PDF' are rejected; requires five
    characters when purely numeric, so a '2026' in a URL path cannot collide
    with a four-digit front-end code; caps length at 24 so the 64-character
    sha256 in every content-addressed WS12 key is never read as a code.
    """
    if not 4 <= len(token) <= 24:
        return False
    if not any(character.isdigit() for character in token):
        return False
    if token.isdigit() and len(token) < 5:
        return False
    return True


def path_tokens(value) -> set[str]:
    """Code-shaped tokens in an S3 key or source URL.

    WS12 keys are content-addressed --
    ws12/{class}/{portal_id}/{sha256[:2]}/{sha256}.pdf, per S3OriginalSink.put
    in mine_file_harvest.py -- so this probe rarely fires on the IGS and AZGS
    keys. It exists for the rows whose source URL still carries the survey
    code, e.g. .../MILS_MRDS/IF0131_001.pdf -> IF0131, which is exactly the
    IF0126 research copy in the acceptance record.

    A token with no letter is never indexed. looks_like_code() admits a bare
    5-digit-or-longer run, and a bare run found anywhere in a path is not
    evidence of anything: stategeo_or.json alone contributes 24,664
    'ID_00001'-style ids and the mrds_*.json files another 124,598 eight-digit
    dep_ids, so a zero-padded sequence number, an accession number or a packed
    date in one corpus URL is enough to write a coincidence at
    CONF_CODE_IN_PATH = 0.9 -- above the 0.8 line an operator filters on. The
    numeric ids still resolve at 1.0 through the mine_ids tier, where the
    match is an equality against a stored id and validates itself.
    """
    found = set()
    for token in TOKEN_SPLIT.split(str(value or '')):
        upper = token.upper()
        if not looks_like_code(upper):
            continue
        if any(letter.isalpha() for letter in upper):
            found.add(upper)
    return found


def looks_like_segment_code(segment: str) -> bool:
    """Is this whole path component shaped like a record code?

    Wider than looks_like_code() because the match has to be the entire
    component rather than a run found somewhere inside one, which is far more
    selective: '91-04-0001' (the form of all 1,975 California state-survey
    ids) and 'ADMM-1552428849304-690' both qualify. A component that is only
    digits still does not: that is a page number or an accession sequence as
    often as it is an id.
    """
    if not 4 <= len(segment) <= 48:
        return False
    if not any(digit.isdigit() for digit in segment):
        return False
    return bool(any(letter.isalpha() for letter in segment)
                or CODE_PUNCT.search(segment))


def path_segments(value) -> set[str]:
    """Whole path components that are themselves shaped like a record code.

    path_tokens() splits on every non-alphanumeric character, so a hyphenated
    id can never be one of its tokens. Verified against the module's own
    helpers: path_tokens() of an AZGS collection URL returns the empty set --
    'ADMM' has no digit and the numeric run has no letter -- which left
    CONF_PREFIX_IN_PATH unreachable for all three ADMM-{code} rules, i.e. for
    the 13,036 AZGS rows those rules exist to serve. mine_file_harvest.py
    _azgs_metadata builds the source URL as
    {collection_file_base}/{collection_id}/{filename}, so the collection id
    that ws13_documents stores as the mine id is a path component verbatim.

    The extension-stripped form is indexed too, for the .../ADMM-0123.pdf
    shape where the code names the file rather than the directory.
    """
    found = set()
    for segment in SEGMENT_SPLIT.split(str(value or '')):
        for candidate in (segment, segment.rsplit('.', 1)[0]):
            upper = candidate.upper()
            if looks_like_segment_code(upper):
                found.add(upper)
    return found


def record_example(documents: list, sha256) -> None:
    """Keep the EXAMPLE_DOCUMENTS smallest distinct sha256s, in sorted order.

    Appending the first five in row order made evidence depend on the scan
    order of a 56,282-row table that the embed and provenance backfills UPDATE
    and that Postgres is free to read with interleaved parallel workers. That
    order lands in the evidence JSONB, and the ON CONFLICT change test
    compares evidence, so an unchanged rerun rewrote thousands of rows and
    reset the updated_at that is supposed to mean "when this mapping last
    changed".
    """
    value = str(sha256 or '')
    if not value or value in documents:
        return
    if len(documents) < EXAMPLE_DOCUMENTS:
        documents.append(value)
    elif value < documents[-1]:
        documents[-1] = value
    else:
        return
    documents.sort()


def trigrams(text: str) -> set[str]:
    """pg_trgm-shaped character trigrams over a normalised name."""
    padded = f'  {text} '
    return {padded[index:index + 3] for index in range(len(padded) - 2)}


def confidence_band(confidence: float) -> str:
    for name, floor in CONFIDENCE_BANDS:
        if confidence >= floor:
            return name
    return CONFIDENCE_BANDS[-1][0]


def load_site_id_file(path: str):
    """Front-end ids from one columnar site file.

    Each record is emitted in both spellings the front end actually uses: the
    stateSurveySafeId slug ('stategeo-igs-dd-1-if0126', which the citation
    path and tools/test_doc_viewer.js use) and the namespaced raw id
    ('stategeo:IGS DD-1 IF0126', which site/data/docs/index.json uses as a
    by_mine key). The bare record id is deliberately not emitted:
    stategeo_wy.json numbers its sites '1', '2', ..., so bare ids are not
    unique across sources and could not be a primary key without silently
    merging two different mines.
    """
    source = os.path.relpath(path, REPO_ROOT)
    with open(path, encoding='utf-8') as handle:
        payload = json.load(handle)
    namespace = str(payload.get('src') or '').strip()
    if not namespace:
        raise ValueError(f'{source} has no src namespace')
    state = str(payload.get('state') or '').strip().upper() or None
    ids = payload.get('id') or []
    names = payload.get('nm') or []
    if not ids:
        # USMIN publishes its points without stable record ids, so the front
        # end cannot address them either. Reported, never silently dropped.
        return [], source
    if names and len(names) != len(ids):
        raise ValueError(f'{source} id and nm columns disagree in length')
    records = []
    for index, raw_id in enumerate(ids):
        record_id = str(raw_id).strip()
        if not record_id:
            raise ValueError(f'{source} row {index} has an empty id')
        name = (str(names[index]).strip() or None) if names else None
        slug = f'{namespace}-{front_end_slug(record_id)}'
        records.append(FrontEndRecord(slug, source, namespace, record_id,
                                      name, state, 'slug'))
        records.append(FrontEndRecord(f'{namespace}:{record_id}', source,
                                      namespace, record_id, name, state,
                                      'namespaced'))
    return records, None


def load_site_id_dir(directory: str):
    """Every columnar site file in a directory."""
    records: list[FrontEndRecord] = []
    without_ids: list[str] = []
    for path in sorted(glob.glob(os.path.join(directory, '*.json'))):
        found, empty = load_site_id_file(path)
        records.extend(found)
        if empty:
            without_ids.append(empty)
    return records, without_ids


def load_docs_index(path: str) -> list[FrontEndRecord]:
    """Front-end ids the published document index already keys on."""
    source = os.path.relpath(path, REPO_ROOT)
    with open(path, encoding='utf-8') as handle:
        payload = json.load(handle)
    documents = {str(row.get('document_id')): row
                 for row in payload.get('documents') or []}
    records = []
    for key, doc_ids in sorted((payload.get('by_mine') or {}).items()):
        state = name = None
        for doc_id in doc_ids or []:
            row = documents.get(str(doc_id))
            if not row:
                continue
            state = state or (str(row.get('state') or '').upper() or None)
            for candidate in row.get('mine_names') or []:
                name = name or (str(candidate).strip() or None)
        records.append(FrontEndRecord(str(key), source, None, str(key), name,
                                      state, 'declared'))
    return records


def load_document_registry(path: str) -> list[FrontEndRecord]:
    """Front-end ids declared by the checked-in WS12 document registry."""
    source = os.path.relpath(path, REPO_ROOT)
    with open(path, encoding='utf-8') as handle:
        payload = json.load(handle)
    records = []
    for document in payload.get('documents') or []:
        subjects = list(document.get('subjects') or [])
        subjects.append({'mine_id': document.get('mine_id'),
                         'state': document.get('state')})
        for subject in subjects:
            mine_id = str(subject.get('mine_id') or '').strip()
            if not mine_id:
                continue
            label = str(subject.get('label') or '').strip()
            # 'St. Louis Mine (IGS DD-1 IF0126), Lava Creek district' -> the
            # name is everything before the parenthesised survey reference.
            name = label.split(' (')[0].strip() or None if label else None
            state = str(subject.get('state') or '').strip().upper() or None
            records.append(FrontEndRecord(mine_id, source, None, mine_id, name,
                                          state, 'declared'))
    return records


def load_id_file(path: str) -> list[FrontEndRecord]:
    """An operator-supplied id list.

    JSON is accepted in the shapes this repository already writes: a columnar
    site file, a document index carrying by_mine, the WS12 document registry,
    a bare list of ids, or {"ids": [...]}. Anything else is read as text, one
    'id[<TAB>name[<TAB>ST]]' per line, '#' comments ignored.
    """
    source = (os.path.relpath(path, REPO_ROOT)
              if os.path.abspath(path).startswith(REPO_ROOT) else path)
    with open(path, encoding='utf-8') as handle:
        text = handle.read()
    stripped = text.lstrip()
    if stripped[:1] in ('{', '['):
        payload = json.loads(text)
        if isinstance(payload, dict):
            if payload.get('by_mine') is not None:
                return load_docs_index(path)
            if payload.get('dataset') == 'ws12-document-registry':
                return load_document_registry(path)
            if payload.get('id') is not None and payload.get('src'):
                return load_site_id_file(path)[0]
            values = payload.get('ids')
        else:
            values = payload
        if values is None:
            raise ValueError(f'{source} has no recognisable front-end id list')
        records = []
        for index, value in enumerate(values):
            identifier = str(value).strip()
            if not identifier:
                # Dropping it silently would make the coverage counts a
                # measurement of a different list than the one handed in, and
                # load_site_id_file raises on exactly this.
                raise ValueError(f'{source} entry {index} has an empty id')
            records.append(FrontEndRecord(identifier, source, None, identifier,
                                          None, None, 'file'))
        return records
    records = []
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        parts = [part.strip() for part in line.split('\t')]
        if not parts[0]:
            # A leading tab -- a mis-aligned column or a copy-paste -- passes
            # the emptiness check above and leaves parts[0] == '', which used
            # to be written as a row whose primary key is the empty string:
            # addressable by nothing, and counted in the coverage numbers the
            # report presents as a measurement. load_site_id_file raises on
            # exactly this; the two loaders now agree.
            raise ValueError(f'{source} line {number} has an empty id')
        name = parts[1] if len(parts) > 1 and parts[1] else None
        state = parts[2].upper() if len(parts) > 2 and parts[2] else None
        records.append(FrontEndRecord(parts[0], source, None, parts[0], name,
                                      state, 'file'))
    return records


def discover_front_end_ids(paths):
    """Read the front-end id list, from --front-end-ids or from the site."""
    provenance = {'sources': [], 'id_sources_without_ids': []}
    records: list[FrontEndRecord] = []
    if paths:
        for path in paths:
            if os.path.isdir(path):
                found, without = load_site_id_dir(path)
                records.extend(found)
                provenance['id_sources_without_ids'].extend(without)
            elif os.path.isfile(path):
                records.extend(load_id_file(path))
            else:
                sys.exit(f'--front-end-ids path does not exist: {path}')
            provenance['sources'].append(path)
        if not records:
            sys.exit('--front-end-ids produced no ids; refusing to write an '
                     'empty mapping over an existing one')
        return records, provenance

    if os.path.isdir(SITE_ID_DIR):
        found, without = load_site_id_dir(SITE_ID_DIR)
        records.extend(found)
        provenance['id_sources_without_ids'].extend(without)
        provenance['sources'].append(os.path.relpath(SITE_ID_DIR, REPO_ROOT))
    if os.path.isfile(DOCS_INDEX):
        records.extend(load_docs_index(DOCS_INDEX))
        provenance['sources'].append(os.path.relpath(DOCS_INDEX, REPO_ROOT))
    if os.path.isfile(DOC_REGISTRY):
        records.extend(load_document_registry(DOC_REGISTRY))
        provenance['sources'].append(os.path.relpath(DOC_REGISTRY, REPO_ROOT))
    if not records:
        sys.exit('no local front-end id list was found (looked for '
                 f'{SITE_ID_DIR}, {DOCS_INDEX} and {DOC_REGISTRY}); '
                 'pass --front-end-ids')
    return records, provenance


def merge_records(records):
    """Collapse duplicate spellings, and flag genuine id collisions.

    Two different site records producing the same front-end id would be merged
    silently by the primary key, so that case is detected here and forced to
    'unmapped' with both record ids in its evidence. Only records that carry a
    namespace take part: those are the ids this module *constructs* from a
    (namespace, record_id) pair, and so the only ones that can collide by
    accident. The current site files produce 179,404 distinct ids with zero
    collisions in either spelling; this exists so a later site build cannot
    introduce one quietly.

    The key includes the declaring file, because the namespace alone does not
    identify a record and the one collision shape the data can already produce
    is the shape (namespace, record_id) misses: every stategeo_*.json declares
    src='stategeo' and stategeo_wy.json numbers its 4,841 sites '1', '2', ...,
    so a later stategeo_nv.json numbered the same way would give record '1' of
    two states the same 'stategeo-1' id, one key string, and one silently
    merged row.
    """
    grouped: dict[str, list[FrontEndRecord]] = collections.OrderedDict()
    for record in records:
        grouped.setdefault(record.front_end_id, []).append(record)
    merged = []
    for group in grouped.values():
        keys = sorted({f'{record.source}|{record.namespace}:{record.record_id}'
                       for record in group if record.namespace})
        best = max(group, key=lambda record: (bool(record.namespace),
                                              bool(record.name),
                                              bool(record.state),
                                              record.source))
        merged.append((best, group, keys if len(keys) > 1 else None))
    return merged


class CorpusIndex:
    """Every way the corpus can be addressed, loaded once into memory.

    56,282 documents is small enough to index in the process (a few tens of
    MB), and indexing avoids the alternative: one similarity() query per
    front-end id, which would be 179,404 round trips against a column with no
    trigram index.

    Paths are indexed twice, by token and by whole component, because the two
    carry different weight: a component equal to the code is evidence, a run
    of characters found somewhere inside a path is much weaker. Both are
    bounded by the handful of components a key or URL has.

    Everything the index collects is order-independent -- the example
    documents are the smallest sha256s, the reported name spelling is the
    smallest, corpus ids are folded to one spelling per code -- because these
    values end up in the evidence JSONB that decides whether a rerun rewrites
    a row.
    """

    def __init__(self, rows, source_url_available: bool):
        self.source_url_available = source_url_available
        self.documents = 0
        self.mine_id_spellings: dict[str, set[str]] = {}
        self.docs_by_mine_id: collections.Counter = collections.Counter()
        self.by_key_token: dict[str, dict] = {}
        self.by_url_token: dict[str, dict] = {}
        self.by_key_segment: dict[str, dict] = {}
        self.by_url_segment: dict[str, dict] = {}
        entries: dict[tuple, dict] = {}
        for sha256, s3_key, state, mine_ids, mine_names, source_url in rows:
            self.documents += 1
            state = (str(state).strip().upper() or None) if state else None
            ids = [str(value).strip() for value in (mine_ids or [])
                   if str(value).strip()]
            for mine_id in ids:
                self.mine_id_spellings.setdefault(
                    mine_id.upper(), set()).add(mine_id)
                self.docs_by_mine_id[mine_id] += 1
            # Case-folded while indexing: 'SP0145' and 'sp0145' are one corpus
            # id recorded twice, and counting them as two candidates made an
            # unambiguous name look ambiguous.
            codes = {mine_id.upper() for mine_id in ids}
            for index, value in ((self.by_key_token, s3_key),
                                 (self.by_url_token, source_url)):
                for token in path_tokens(value):
                    self._record_token(index, token, sha256, codes)
            for index, value in ((self.by_key_segment, s3_key),
                                 (self.by_url_segment, source_url)):
                for segment in path_segments(value):
                    self._record_token(index, segment, sha256, codes)
            for raw_name in mine_names or []:
                norm = normalize_name(raw_name)
                if not norm:
                    continue
                entry = entries.setdefault((norm, state), {
                    'raw': None, 'mine_ids': set(), 'documents': [],
                    'doc_count': 0})
                entry['mine_ids'].update(codes)
                entry['doc_count'] += 1
                # The lexicographically first spelling, not the first one this
                # scan happened to see: the raw name is reported as evidence
                # and evidence is what the ON CONFLICT change test compares.
                spelling = str(raw_name).strip()
                if spelling and (entry['raw'] is None
                                 or spelling < entry['raw']):
                    entry['raw'] = spelling
                record_example(entry['documents'], sha256)
        for index in (self.by_key_token, self.by_url_token,
                      self.by_key_segment, self.by_url_segment):
            for slot in index.values():
                slot['mine_ids'] = self.canonical_ids(slot['mine_ids'])
        self.name_entries: list[NameEntry] = []
        self.entry_by_key: dict[tuple, int] = {}
        self.entry_by_norm: dict[str, list] = {}
        self.trigram_by_state: dict = {}
        for (norm, state), entry in entries.items():
            index = len(self.name_entries)
            self.name_entries.append(NameEntry(
                norm, entry['raw'] or norm, state,
                self.canonical_ids(entry['mine_ids']),
                tuple(entry['documents']), entry['doc_count']))
            self.entry_by_key[(norm, state)] = index
            self.entry_by_norm.setdefault(norm, []).append(index)
            postings = self.trigram_by_state.setdefault(state, {})
            for gram in trigrams(norm):
                postings.setdefault(gram, []).append(index)
        self.states_with_names = {state for state in self.trigram_by_state
                                  if state is not None}
        self.entries_without_state = sum(1 for entry in self.name_entries
                                         if entry.state is None)
        self._fuzzy_cache: dict = {}

    @staticmethod
    def _record_token(index, token, sha256, mine_ids):
        slot = index.setdefault(token, {'mine_ids': set(), 'documents': []})
        slot['mine_ids'].update(mine_ids)
        record_example(slot['documents'], sha256)

    def resolve_spellings(self, mine_id):
        """Canonical spelling, every stored spelling, and the document count.

        ws13_documents.mine_ids keeps the raw survey attribute -- _seed_igs
        reads IGSID straight off the ArcGIS record -- so one corpus id can be
        stored as both 'SP0145' and 'sp0145'. ws13_documents_mines is a GIN
        index over the exact strings, so a row naming one spelling cannot
        reach the documents filed under the other; every spelling is returned
        for ws13_mine_id_all and the count sums over all of them rather than
        reporting the one spelling's share as if it were the total.

        The canonical spelling is the one most documents use, ties broken
        lexicographically so the answer does not depend on scan order.
        """
        spellings = self.mine_id_spellings.get(str(mine_id).upper())
        if not spellings:
            return None, (), 0
        ordered = tuple(sorted(spellings))
        canonical = min(
            ordered, key=lambda value: (-self.docs_by_mine_id[value], value))
        documents = sum(self.docs_by_mine_id[value] for value in ordered)
        return canonical, ordered, documents

    def canonical_ids(self, codes) -> tuple:
        """Case-folded corpus codes -> their canonical spellings, sorted."""
        return tuple(sorted({self.resolve_spellings(code)[0] or str(code)
                             for code in codes}))

    def _postings_for(self, state, cross_state: bool):
        if state is None or cross_state:
            return list(self.trigram_by_state.values())
        postings = self.trigram_by_state.get(state)
        return [postings] if postings else []

    def fuzzy_name(self, name, state, min_ratio, cross_state=False):
        """Best (entry, ratio) for a front-end name, or (None, FuzzyMiss).

        State blocking is strict by default: a name is only compared against
        corpus names recorded for the same state. The tier is capped at 0.6
        and never verified, so a cross-state name coincidence is not worth
        either the precision loss or the sweep; --fuzzy-cross-state opens it
        up when the report shows enough corpus rows with an unknown state to
        justify it.
        """
        norm = normalize_name(name)
        if not norm:
            return None, FuzzyMiss('front_end_name_missing')
        cache_key = (norm, state)
        if cache_key in self._fuzzy_cache:
            return self._fuzzy_cache[cache_key]
        result = self._fuzzy_uncached(norm, state, min_ratio, cross_state)
        self._fuzzy_cache[cache_key] = result
        return result

    def _merge_entries(self, indexes):
        """One entry standing for several, or None when they disagree.

        Several corpus (name, state) rows can share a normalised name --
        'copper king' is recorded in AZ, ID and MT. If they all resolve to the
        same corpus mine id (or to none) the choice between them does not
        change the answer and they are merged; if they resolve to different
        ids the caller has to refuse, because picking one is exactly the
        silent mis-attribution the retrieval path would then serve.
        """
        entries = sorted((self.name_entries[index] for index in indexes),
                         key=lambda entry: (-entry.doc_count, entry.norm,
                                            entry.state or '', entry.raw))
        mine_ids = sorted({mine_id for entry in entries
                           for mine_id in entry.mine_ids})
        if len(mine_ids) > 1:
            return None
        documents: list = []
        for entry in entries:
            for sha256 in entry.documents:
                record_example(documents, sha256)
        states = {entry.state for entry in entries}
        head = entries[0]
        return NameEntry(head.norm, head.raw,
                         states.pop() if len(states) == 1 else None,
                         tuple(mine_ids), tuple(documents),
                         sum(entry.doc_count for entry in entries))

    def describe_entries(self, indexes) -> tuple:
        """The candidates behind an ambiguous refusal, for the row evidence."""
        return tuple({'name': self.name_entries[index].raw,
                      'state': self.name_entries[index].state,
                      'ws13_documents': self.name_entries[index].doc_count,
                      'mine_ids': list(self.name_entries[index].mine_ids[:8])}
                     for index in sorted(indexes))

    def _fuzzy_uncached(self, norm, state, min_ratio, cross_state):
        postings_sets = self._postings_for(state, cross_state)
        if not postings_sets:
            return None, FuzzyMiss('no_corpus_names_for_state')
        exact = self.entry_by_key.get((norm, state))
        if exact is None and (state is None or cross_state):
            # entry_by_norm is in ws13_documents scan order, so taking [0] here
            # returned one of several identically-named mines at ratio 1.0 --
            # a run-to-run coin flip presented as a certainty. Every operator
            # id list (`id<TAB>name`) has state=None, so this is the normal
            # path for one, not a corner.
            candidates = self.entry_by_norm.get(norm) or []
            if len(candidates) == 1:
                exact = candidates[0]
            elif candidates:
                merged = self._merge_entries(candidates)
                if merged is None:
                    return None, FuzzyMiss(
                        'exact_name_ambiguous_across_states',
                        self.describe_entries(candidates))
                return (merged, 1.0), None
        if exact is not None:
            return (self.name_entries[exact], 1.0), None
        counts: collections.Counter = collections.Counter()
        grams = trigrams(norm)
        for postings in postings_sets:
            lists = [postings[gram] for gram in grams
                     if gram in postings
                     and len(postings[gram]) <= TRIGRAM_POSTING_CAP]
            # Only the most selective grams are scanned; a common gram adds
            # thousands of candidates and almost no discrimination.
            for posting in heapq.nsmallest(TRIGRAM_PROBES, lists, key=len):
                counts.update(posting)
        if not counts:
            return None, FuzzyMiss('no_trigram_candidates')
        scored = []
        shortlist = heapq.nlargest(FUZZY_CANDIDATES, counts.items(),
                                   key=lambda item: item[1])
        for index, _shared in shortlist:
            entry = self.name_entries[index]
            matcher = difflib.SequenceMatcher(None, norm, entry.norm)
            # quick_ratio is a cheap upper bound; skip the real comparison for
            # candidates that cannot reach the cutoff anyway.
            if matcher.quick_ratio() < min_ratio:
                continue
            # Rounded before the comparison: two candidates separated at the
            # thirteenth decimal are a tie, and `>` on the raw float made the
            # winner depend on the order the shortlist happened to be built in.
            ratio = round(matcher.ratio(), 6)
            if ratio >= min_ratio:
                scored.append((ratio, index))
        if not scored:
            return None, FuzzyMiss('below_min_fuzzy_ratio')
        best_ratio = max(ratio for ratio, _index in scored)
        tied = [index for ratio, index in scored if ratio == best_ratio]
        if len(tied) == 1:
            return (self.name_entries[tied[0]], best_ratio), None
        merged = self._merge_entries(tied)
        if merged is None:
            return None, FuzzyMiss('fuzzy_name_tied_between_mines',
                                   self.describe_entries(tied))
        return (merged, best_ratio), None


def code_candidates(record: FrontEndRecord) -> list[str]:
    """Codes the front-end id might carry, most specific first."""
    candidates: list[str] = []
    for value in (record.record_id, record.front_end_id):
        if not value:
            continue
        upper = str(value).strip().upper()
        # A whole record id with whitespace ('IGS DD-1 IF0126') is a label,
        # not a code; only its trailing token can be one.
        if upper and not WHITESPACE.search(upper) and upper not in candidates:
            candidates.append(upper)
        tokens = [token for token in TOKEN_SPLIT.split(upper) if token]
        if tokens and tokens[-1] not in candidates:
            candidates.append(tokens[-1])
    return [code for code in candidates if looks_like_code(code)]


def path_hit(corpus: CorpusIndex, code: str, attempts: list):
    """Resolve a code seen in an s3_key or source_url, or explain why not.

    Whole path components are probed before tokens: a component equal to the
    code is far more selective than a run of characters found somewhere in a
    path. A code that is only digits is never probed at all -- see path_tokens
    for the 149,262 front-end ids that would otherwise probe naked numbers.
    """
    has_letter = any(letter.isalpha() for letter in code)
    if not has_letter and not CODE_PUNCT.search(code):
        attempts.append(f'path:{code}:numeric_code_not_probed')
        return None, None, None, ()
    for label, kind, index in (('s3_key', 'segment', corpus.by_key_segment),
                               ('s3_key', 'token', corpus.by_key_token),
                               ('source_url', 'segment',
                                corpus.by_url_segment),
                               ('source_url', 'token', corpus.by_url_token)):
        slot = index.get(code)
        if not slot:
            continue
        mine_ids = slot['mine_ids']
        if len(mine_ids) == 1:
            return label, kind, mine_ids[0], slot['documents']
        if not mine_ids:
            # The documents exist but carry no mine id at all: the content
            # bridge is real and the identifier bridge still is not. Recorded
            # so an operator can see it, never guessed.
            attempts.append(
                f'{label}:{kind}:{code}:documents_without_mine_ids:'
                + ','.join(slot['documents'][:3]))
        else:
            attempts.append(f'{label}:{kind}:{code}:ambiguous:'
                            + ','.join(mine_ids[:8]))
    return None, None, None, ()


def derive(record, group, collision, corpus, min_ratio, cross_state=False):
    """Map one front-end id, in strict precedence order."""
    base = {'front_end_id': record.front_end_id, 'id_form': record.id_form,
            'source': record.source, 'declarations': len(group)}
    if record.namespace:
        base['front_end_namespace'] = record.namespace
    if record.record_id and record.record_id != record.front_end_id:
        base['front_end_record_id'] = record.record_id
    if record.name:
        base['front_end_name'] = record.name
    if record.state:
        base['state'] = record.state
    if collision:
        # Two different site records claim the same front-end id. Mapping
        # either would silently merge two mines under one primary key.
        return Mapping(None, None, 'unmapped', 0.0, False,
                       dict(base, reason='front_end_id_collision',
                            colliding_record_ids=collision,
                            sources=sorted({row.source for row in group})))

    attempts: list[str] = []

    for code in code_candidates(record):
        canonical, spellings, documents = corpus.resolve_spellings(code)
        if canonical:
            evidence = dict(base, matched_in='mine_ids', code=code,
                            ws13_documents=documents)
            if len(spellings) > 1:
                evidence['ws13_mine_id_spellings'] = list(spellings)
            if attempts:
                evidence['attempts'] = attempts
            return Mapping(canonical, list(spellings), 'embedded_code',
                           CONF_CODE_IN_MINE_IDS, True, evidence)
        attempts.append(f'mine_ids:{code}:no_match')
        label, kind, mine_id, examples = path_hit(corpus, code, attempts)
        if mine_id:
            canonical, spellings, documents = corpus.resolve_spellings(mine_id)
            evidence = dict(base, matched_in=label, matched_by=kind, code=code,
                            ws13_documents=documents,
                            example_documents=list(examples),
                            attempts=attempts)
            if len(spellings) > 1:
                evidence['ws13_mine_id_spellings'] = list(spellings)
            return Mapping(canonical, list(spellings), 'embedded_code',
                           CONF_CODE_IN_PATH, False, evidence)

    probe = front_end_slug(record.front_end_id)
    for rule in NAMESPACE_RULES:
        if not probe.startswith(rule.prefix):
            continue
        remainder = probe[len(rule.prefix):].upper()
        if not remainder:
            attempts.append(f'prefix:{rule.prefix}:empty_code')
            continue
        if rule.code_pattern and not re.fullmatch(rule.code_pattern,
                                                  remainder):
            attempts.append(f'prefix:{rule.prefix}{remainder}:shape_mismatch')
            continue
        candidate = rule.template.format(code=remainder)
        canonical, spellings, documents = corpus.resolve_spellings(candidate)
        if canonical:
            evidence = dict(base, matched_in='mine_ids',
                            front_end_prefix=rule.prefix,
                            corpus_candidate=candidate, rule=rule.note,
                            ws13_documents=documents, attempts=attempts)
            if len(spellings) > 1:
                evidence['ws13_mine_id_spellings'] = list(spellings)
            return Mapping(canonical, list(spellings), 'prefix_namespace',
                           CONF_PREFIX_IN_MINE_IDS, True, evidence)
        attempts.append(f'prefix:{rule.prefix}:{candidate}:no_match')
        label, kind, mine_id, examples = path_hit(corpus, candidate, attempts)
        if mine_id:
            canonical, spellings, documents = corpus.resolve_spellings(mine_id)
            evidence = dict(base, matched_in=label, matched_by=kind,
                            front_end_prefix=rule.prefix,
                            corpus_candidate=candidate, rule=rule.note,
                            ws13_documents=documents,
                            example_documents=list(examples),
                            attempts=attempts)
            if len(spellings) > 1:
                evidence['ws13_mine_id_spellings'] = list(spellings)
            return Mapping(canonical, list(spellings), 'prefix_namespace',
                           CONF_PREFIX_IN_PATH, False, evidence)

    if not record.name:
        attempts.append('fuzzy_name:front_end_name_missing')
        return Mapping(None, None, 'unmapped', 0.0, False,
                       dict(base, reason='no_evidence', attempts=attempts))

    hit, miss = corpus.fuzzy_name(record.name, record.state, min_ratio,
                                  cross_state)
    if hit is None:
        attempts.append(f'fuzzy_name:{miss.reason}')
        # An ambiguous refusal names what it refused between; a plain miss has
        # nothing to show, and 'no_evidence' is what the report counts.
        evidence = dict(base, attempts=attempts,
                        reason=miss.reason if miss.candidates
                        else 'no_evidence')
        if miss.candidates:
            evidence['candidate_names'] = list(miss.candidates)
        return Mapping(None, None, 'unmapped', 0.0, False, evidence)

    entry, ratio = hit
    confidence = round(ratio * FUZZY_CEILING, 4)
    state_match = 'exact'
    if record.state is None or entry.state != record.state:
        confidence = round(confidence * UNKNOWN_STATE_FACTOR, 4)
        if record.state is None:
            state_match = 'front_end_state_unknown'
        elif entry.state is None:
            state_match = 'corpus_state_unknown'
        else:
            state_match = 'cross_state'
    common = dict(base, matched_name=entry.raw,
                  matched_name_normalized=entry.norm, ratio=round(ratio, 4),
                  state_match=state_match, corpus_state=entry.state,
                  matched_name_documents=entry.doc_count,
                  example_documents=list(entry.documents))
    if len(entry.mine_ids) == 1:
        canonical, spellings, documents = corpus.resolve_spellings(
            entry.mine_ids[0])
        evidence = dict(common, matched_in='mine_names',
                        ws13_documents=documents, attempts=attempts)
        if len(spellings) > 1:
            evidence['ws13_mine_id_spellings'] = list(spellings)
        return Mapping(canonical, list(spellings), 'fuzzy_name', confidence,
                       False, evidence)
    # A name that resolves to several corpus mine ids, or to none at all, is
    # evidence of a document but not of an identifier. Recorded, not guessed.
    reason = ('fuzzy_name_without_mine_id' if not entry.mine_ids
              else 'fuzzy_name_ambiguous')
    attempts.append(f'fuzzy_name:{reason}:' + ','.join(entry.mine_ids[:8]))
    return Mapping(None, None, 'unmapped', 0.0, False,
                   dict(common, reason=reason,
                        candidate_mine_ids=list(entry.mine_ids[:8]),
                        attempts=attempts))


def load_corpus(conn) -> CorpusIndex:
    # Resolved through to_regclass so the column probe and the SELECT below
    # can never disagree about which schema's ws13_documents is in play.
    columns = {row[0] for row in conn.execute(
        'SELECT attname FROM pg_attribute '
        "  WHERE attrelid = to_regclass('ws13_documents')"
        '    AND attnum > 0 AND NOT attisdropped').fetchall()}
    if not columns:
        sys.exit('ws13_documents is not visible on this connection')
    source_url_available = 'source_url' in columns
    if not source_url_available:
        # Loud, not silent: source_url is the probe that recovers the
        # IF0131_001.pdf research copy, so its absence has to be reported
        # rather than quietly halving this tier.
        print('WARNING: ws13_documents.source_url is missing, so the '
              'source_url probe is disabled for this run; apply the WS12 '
              'rights/URL migration first', file=sys.stderr)
    select_url = 'source_url' if source_url_available else 'NULL::text'
    # ORDER BY sha256 is not cosmetic. The example_documents and matched_name
    # this index collects are scan-order dependent, they land in the evidence
    # JSONB, and the ON CONFLICT change test compares evidence -- so on a
    # 56,282-row table that the backfills UPDATE and that Postgres may read
    # with interleaved parallel workers, an unchanged rerun rewrote thousands
    # of rows and reset updated_at. The collectors are order-independent as
    # well now; this makes the input order fixed too.
    rows = conn.execute(
        'SELECT sha256, s3_key, state, mine_ids, mine_names, '
        f'       {select_url} '
        '  FROM ws13_documents '
        ' ORDER BY sha256').fetchall()
    return CorpusIndex(rows, source_url_available)


def load_existing(conn) -> dict:
    """front_end_id -> verified, for rows already in the map."""
    if not conn.execute("SELECT to_regclass('ws13_mine_id_map')"
                        ).fetchone()[0]:
        return {}
    return {row[0]: bool(row[1]) for row in conn.execute(
        'SELECT front_end_id, verified FROM ws13_mine_id_map').fetchall()}


def has_pg_trgm(conn) -> bool:
    return bool(conn.execute(
        "SELECT 1 FROM pg_extension WHERE extname = 'pg_trgm'").fetchone())


def table_exists(conn) -> bool:
    return bool(conn.execute("SELECT to_regclass('ws13_mine_id_map')"
                             ).fetchone()[0])


def verify_table_shape(conn) -> list:
    """Every way the live ws13_mine_id_map differs from CREATE_TABLE_SQL.

    CREATE TABLE IF NOT EXISTS is silent about a table that already exists
    with the wrong definition, which is the hazard ws13_migrations.sql adds an
    explicit DO $guard$ block for. A ws13_mine_id_map left by a hand-run or by
    an earlier revision of this script has no ws13_mine_id_all column and none
    of the CHECK constraints that make a fuzzy guess unrepresentable as
    verified -- and the INSERT would still succeed, so the run would report
    success while the guarantees the tiers rest on were simply absent.
    """
    columns = {row[0]: (row[1], bool(row[2])) for row in conn.execute(
        'SELECT attname, format_type(atttypid, atttypmod), attnotnull '
        '  FROM pg_attribute '
        "  WHERE attrelid = to_regclass('ws13_mine_id_map')"
        '    AND attnum > 0 AND NOT attisdropped').fetchall()}
    problems = []
    for name, expected, not_null in EXPECTED_COLUMNS:
        actual = columns.get(name)
        if actual is None:
            problems.append(f'column {name} is missing')
            continue
        if actual[0] != expected:
            problems.append(
                f'column {name} is {actual[0]}, expected {expected}')
        if actual[1] != not_null:
            problems.append(f'column {name} is '
                            + ('NOT NULL' if actual[1] else 'nullable')
                            + ', expected '
                            + ('NOT NULL' if not_null else 'nullable'))
    constraints = {row[0]: row[1] for row in conn.execute(
        'SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint '
        "  WHERE conrelid = to_regclass('ws13_mine_id_map')").fetchall()}
    for name in EXPECTED_CONSTRAINTS:
        if name not in constraints:
            problems.append(f'constraint {name} is missing')
    primary = sorted(definition for definition in constraints.values()
                     if definition.startswith('PRIMARY KEY'))
    if primary != [EXPECTED_PRIMARY_KEY]:
        problems.append('primary key is '
                        + (', '.join(primary) or 'missing')
                        + f', expected {EXPECTED_PRIMARY_KEY}')
    return problems


def refuse_wrong_shape(problems) -> None:
    """Stop rather than write through a table with a different definition."""
    sys.exit('ws13_mine_id_map exists with a different shape:\n  '
             + '\n  '.join(problems)
             + '\nRefusing to write through it: this script rebuilds the map '
               'from scratch, so DROP TABLE ws13_mine_id_map (or ALTER it to '
               'the definition in CREATE_TABLE_SQL) and rerun.')


def ensure_table(conn):
    """Create the table and let the retrieval role read it.

    ws13_migrations.sql grants ws13_reader SELECT on every existing ws13_*
    table and sets ALTER DEFAULT PRIVILEGES for later ones, but this table is
    created after that migration and possibly by a different role, in which
    case the default privileges do not reach it. The explicit grant is a
    no-op when they did, and the difference between a working bridge and an
    invisible one when they did not. Call under autocommit so a refused grant
    cannot poison the upsert transaction.

    The shape is verified after the CREATE, not assumed from it: see
    verify_table_shape. main() runs the same check before deriving 358,808
    rows so the refusal costs a connection rather than a full run.
    """
    conn.execute(CREATE_TABLE_SQL)
    problems = verify_table_shape(conn)
    if problems:
        refuse_wrong_shape(problems)
    if not conn.execute("SELECT 1 FROM pg_roles WHERE rolname = 'ws13_reader'"
                        ).fetchone():
        return None
    try:
        conn.execute('GRANT SELECT ON ws13_mine_id_map TO ws13_reader')
    except psycopg.Error as exc:
        # The retrieval Lambda reads as ws13_reader; without the grant the
        # bridge exists and is invisible to the only process that needs it.
        print('WARNING: could not GRANT SELECT on ws13_mine_id_map to '
              f'ws13_reader ({exc}); an owner must grant it before the '
              'retrieval Lambda can use the bridge', file=sys.stderr)
        return False
    return True


def write_rows(conn, rows, existing, stamp, batch_size):
    """Upsert every derived row; the SQL refuses verified downgrades."""
    payload = [(front_end_id, mapping.ws13_mine_id, mapping.ws13_mine_id_all,
                mapping.method, mapping.confidence, mapping.verified,
                Jsonb(mapping.evidence), stamp)
               for front_end_id, mapping in rows]
    with conn.cursor() as cursor:
        for start in range(0, len(payload), batch_size):
            cursor.executemany(UPSERT_SQL, payload[start:start + batch_size])
            conn.commit()
            print(f'  upserted {min(start + batch_size, len(payload))}'
                  f'/{len(payload)}')
    changed = conn.execute(
        'SELECT count(*) FROM ws13_mine_id_map WHERE updated_at = %s',
        (stamp,)).fetchone()[0]
    total = conn.execute('SELECT count(*) FROM ws13_mine_id_map').fetchone()[0]
    return {'rows_submitted': len(payload), 'rows_changed': changed,
            'rows_in_table': total,
            'rows_not_in_this_run': total - len(payload),
            'verified_rows_preserved': preserved_count(rows, existing)}


def preserved_count(rows, existing) -> int:
    """Rows the ON CONFLICT guard refuses to downgrade."""
    return sum(1 for front_end_id, mapping in rows
               if existing.get(front_end_id) and not mapping.verified)


def prepare_report_path(path: str) -> None:
    """Prove --report is writable before any work starts.

    The report was opened for the first time after the upsert had committed,
    so an unwritable path threw away the only record of a run that derives
    358,808 rows and writes them in batches -- and var/ is untracked, so
    `--report var/ws13/mine-id-map.json` on a fresh checkout is exactly that
    case. Every other argument is validated at the top of main(); this one is
    now too. Append mode on purpose: the probe must not truncate a report the
    operator still needs if this run fails later.
    """
    parent = os.path.dirname(os.path.abspath(path))
    try:
        os.makedirs(parent, exist_ok=True)
        with open(path, 'a', encoding='utf-8'):
            pass
    except OSError as exc:
        sys.exit(f'--report path is not writable: {exc}')


def write_report(path: str, summary: dict) -> None:
    """Write the summary, or print it rather than lose it."""
    try:
        with open(path, 'w', encoding='utf-8') as handle:
            json.dump(summary, handle, indent=1, sort_keys=True)
            handle.write('\n')
    except OSError as exc:
        # By the time this runs the write has committed; the summary is the
        # only record of what changed, so it goes to stdout rather than away.
        print(f'WARNING: could not write {path} ({exc}); the summary follows '
              'on stdout', file=sys.stderr)
        print(json.dumps(summary, indent=1, sort_keys=True))
        return
    print(f'report written to {path}')


def print_samples(rows, limit):
    """One sample block per tier, so the fuzzy tier is reviewable pre-apply."""
    by_method: dict = {method: [] for method in METHODS}
    for front_end_id, mapping in rows:
        bucket = by_method.setdefault(mapping.method, [])
        if len(bucket) < limit:
            bucket.append((front_end_id, mapping))
    for method in METHODS:
        bucket = by_method.get(method) or []
        print(f'  {method}: showing {len(bucket)}')
        for front_end_id, mapping in bucket:
            evidence = mapping.evidence
            detail = evidence.get('matched_in') or evidence.get('reason') or ''
            extra = ''
            if mapping.method == 'fuzzy_name':
                extra = (f" name={evidence.get('front_end_name')!r}"
                         f" -> {evidence.get('matched_name')!r}"
                         f" ratio={evidence.get('ratio')}"
                         f" state={evidence.get('state_match')}")
            print(f'    {front_end_id} -> {mapping.ws13_mine_id} '
                  f'conf={mapping.confidence} verified={mapping.verified} '
                  f'{detail}{extra}')


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--dsn', default=os.environ.get('WS13_DB_DSN'),
                        help='Postgres DSN (default: $WS13_DB_DSN)')
    parser.add_argument('--front-end-ids', action='append',
                        help='file or directory of front-end mine ids '
                             '(repeatable; default: the site id list under '
                             'build-inputs/data/sites plus the published '
                             'document index and the WS12 document registry)')
    parser.add_argument('--min-fuzzy-ratio', type=float, default=0.88,
                        help='difflib cutoff for the fuzzy_name tier')
    parser.add_argument('--fuzzy-cross-state', action='store_true',
                        help='let fuzzy_name compare across states; off by '
                             'default because the tier is capped at 0.6 and '
                             'never verified')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--dry-run', action='store_true', default=True,
                      help='derive and report without writing (the default)')
    mode.add_argument('--apply', action='store_true',
                      help='create the table if needed and write the rows')
    parser.add_argument('--report', help='write the JSON summary to this path')
    parser.add_argument('--sample', type=int, default=8,
                        help='rows to print per method tier')
    parser.add_argument('--batch', type=int, default=5000,
                        help='rows per upsert batch')
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    if not args.dsn:
        sys.exit('need --dsn (or WS13_DB_DSN)')
    if not 0.0 < args.min_fuzzy_ratio <= 1.0:
        sys.exit('--min-fuzzy-ratio must be greater than 0 and at most 1')
    if args.sample < 0 or args.batch < 1:
        sys.exit('--sample must be >= 0 and --batch >= 1')
    if args.report:
        prepare_report_path(args.report)

    records, provenance = discover_front_end_ids(args.front_end_ids)
    merged = merge_records(records)
    collisions = [record.front_end_id for record, _group, collision in merged
                  if collision]
    print(f'front-end ids: {len(merged)} distinct from {len(records)} '
          f'declarations, sources: {", ".join(provenance["sources"])}')
    if provenance['id_sources_without_ids']:
        print('  note: no record ids published by '
              + ', '.join(provenance['id_sources_without_ids'])
              + ' (those points are not addressable by the front end either)')
    if collisions:
        print(f'  WARNING: {len(collisions)} front-end ids collide and are '
              f'forced to unmapped, e.g. {collisions[:3]}', file=sys.stderr)

    conn = psycopg.connect(args.dsn, autocommit=True)
    trgm = has_pg_trgm(conn)
    # Before the corpus load and the derivation, not after: refusing a
    # mis-shaped table should cost a connection, not a full run.
    shape_problems = verify_table_shape(conn) if table_exists(conn) else []
    if shape_problems and args.apply:
        refuse_wrong_shape(shape_problems)
    if shape_problems:
        print('WARNING: ws13_mine_id_map exists with a different shape and '
              f'--apply would refuse it: {"; ".join(shape_problems)}',
              file=sys.stderr)
    corpus = load_corpus(conn)
    # Not read through a table whose columns are not the ones this script
    # writes: a missing `verified` would turn the dry run into a traceback
    # instead of the message above.
    existing = {} if shape_problems else load_existing(conn)
    print(f'corpus: {corpus.documents} documents, '
          f'{len(corpus.mine_id_spellings)} distinct mine ids, '
          f'{len(corpus.name_entries)} distinct (name, state) pairs '
          f'({corpus.entries_without_state} with no state), source_url probe '
          + ('on' if corpus.source_url_available else 'OFF'))
    # pg_trgm is checked because the task allows using it, and deliberately
    # not used: scoring 179,404 front-end ids through similarity() would be
    # one round trip each against a column with no trigram index, and creating
    # that index is not this script's call to make.
    print(f'  pg_trgm extension present: {trgm} '
          '(matching uses stdlib difflib either way)')
    print(f'  existing map rows: {len(existing)} '
          f'({sum(existing.values())} verified)'
          + (' -- not read, table shape differs' if shape_problems else ''))

    rows = []
    by_method: collections.Counter = collections.Counter()
    by_band: collections.Counter = collections.Counter()
    by_unmapped_reason: collections.Counter = collections.Counter()
    for index, (record, group, collision) in enumerate(merged, start=1):
        mapping = derive(record, group, collision, corpus,
                         args.min_fuzzy_ratio, args.fuzzy_cross_state)
        rows.append((record.front_end_id, mapping))
        by_method[mapping.method] += 1
        by_band[confidence_band(mapping.confidence)] += 1
        if mapping.method == 'unmapped':
            # An id refused for ambiguity is a different thing from an id the
            # corpus has never heard of, and the retrieval path treats them
            # differently; the report has to be able to tell them apart.
            reason = mapping.evidence.get('reason') or 'unknown'
            by_unmapped_reason[reason] += 1
        if index % 25000 == 0:
            print(f'  derived {index}/{len(merged)}')

    mapped = len(rows) - by_method['unmapped']
    print(f'derived {len(rows)} rows: {mapped} mapped, '
          f'{by_method["unmapped"]} unmapped')
    print('  by method:', dict(sorted(by_method.items())))
    print('  by confidence band:', dict(sorted(by_band.items())))
    print('  by unmapped reason:', dict(sorted(by_unmapped_reason.items())))
    print('samples:')
    print_samples(rows, args.sample)

    summary = {
        'generated': dt.datetime.now(dt.timezone.utc).isoformat(),
        'applied': bool(args.apply),
        'min_fuzzy_ratio': args.min_fuzzy_ratio,
        'fuzzy_state_blocking': ('cross_state' if args.fuzzy_cross_state
                                 else 'strict'),
        'front_end': {
            'sources': provenance['sources'],
            'id_sources_without_ids': provenance['id_sources_without_ids'],
            'declarations_read': len(records),
            'distinct_ids': len(merged),
            'collisions': len(collisions),
        },
        'corpus': {
            'documents': corpus.documents,
            'distinct_mine_ids': len(corpus.mine_id_spellings),
            'distinct_name_state_pairs': len(corpus.name_entries),
            'name_entries_without_state': corpus.entries_without_state,
            'source_url_probe': corpus.source_url_available,
            'pg_trgm_present': trgm,
            'matcher': 'stdlib difflib.SequenceMatcher',
        },
        'by_method': dict(sorted(by_method.items())),
        'by_confidence_band': dict(sorted(by_band.items())),
        'by_unmapped_reason': dict(sorted(by_unmapped_reason.items())),
        'existing_rows': len(existing),
        'existing_verified_rows': sum(existing.values()),
        'table_shape_problems': shape_problems,
    }

    if args.apply:
        summary['grant_select_to_ws13_reader'] = ensure_table(conn)
        stamp = dt.datetime.now(dt.timezone.utc)
        conn.autocommit = False
        summary['write'] = write_rows(conn, rows, existing, stamp, args.batch)
        conn.commit()
        print('write:', json.dumps(summary['write'], sort_keys=True))
    else:
        would_preserve = preserved_count(rows, existing)
        summary['write'] = {'dry_run': True, 'rows_submitted': len(rows),
                            'verified_rows_preserved': would_preserve}
        print(f'dry run: nothing written. {len(rows)} rows would be upserted '
              f'and {would_preserve} verified rows would be preserved. '
              'Pass --apply to write.')

    if args.report:
        write_report(args.report, summary)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
