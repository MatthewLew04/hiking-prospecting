#!/usr/bin/env python3
"""Rescue the WS13 documents that produced no text, by their recorded cause.

The handoff said the residual failures were 509-megapixel map plates hitting
tesseract's "Image too large" and prescribed reclassifying them out of
ocr_queue. ws13_manifest disagrees. Seven documents are not 'done' today --
all ocr_queue, all pages=NULL, none with a row in ws13_documents, so between
them they contributed zero chunks -- and what their `error` column actually
records is:

    ocr_exit_4 x5   87473fe10d90 a0d005469420 cb908a113d34 e1f8d25d58b6
                    f3f199e9f71c
    ocr_exit_7 x2   5a3fed772044 (status error)
                    5c991bfa4e90 (status running, stuck, never reaped)

Exit 4 is INVALID_OUTPUT_PDF: ocrmypdf OCRed the pages and then failed its
OWN PDF/A validation on the way out. That is five of the seven, it is not a
tesseract failure, and no amount of reclassification touches it. The remedy
is --output-type pdf, which skips the PDF/A step entirely -- WS13 indexes the
text layer, and PDF/A conformance buys this corpus nothing. Only the two
exit-7 documents (CHILD_PROCESS_ERROR: tesseract crashed or was killed)
failed INSIDE tesseract at all -- the others print tesseract warnings on
their way to a different failure -- and 'Image too large', the message the
handoff generalised from, was recorded for exactly one of them
(5c991bfa4e90, at 52454x9707 = 509 MP, per e9c662a).

So this is a ladder per recorded cause, not one blanket retry:

  * exit 4 / 10       --output-type pdf, then --optimize 0 with
                      --max-image-mpixels 900 (e9c662a recovered
                      ff551ca683e6 exactly that way), then --force-ocr.
  * exit 7            a larger --tesseract-timeout, then
                      --tesseract-pagesegmode alternatives, then --skip-big
                      so the offending page is lost instead of the document.
  * 'invalid jpeg data reading stream' anywhere in the stderr tail, WHATEVER
                      the exit code: the image stream is damaged and no OCR
                      setting reaches it. Repair the input first (qpdf
                      --decode-level=all, then a ghostscript rewrite).
  * 'improbable aspect ratio' / 'facing' / 'Too few characters'
                      page-geometry warnings; the worker already passes
                      --rotate-pages --deskew, so the rungs those add are a
                      lower rotate threshold and a page-segmentation change.

Blast radius, which is why this tool is allowed to re-OCR at all: the
governing WS13 rule is that replacing OCR text forces re-chunking, which
forces re-embedding, which invalidates ws13_chunks_titan_hnsw for those
chunks. These seven documents have no chunks, no ws13_pages rows and no
ws13_documents row -- they are pure loss -- so re-OCRing them cannot
invalidate an embedding or an index entry. Nothing here measures or rewrites
confidence; that pass is somebody else's and runs over all 323,059 OCR pages
first.

One indexing path, not two. A document this tool rescues is NOT written into
ws13_chunks here. The rescue only establishes WHICH remedy produces text;
the document then re-enters through pipelines/ws13_enqueue.py exactly like
every other document, so chunking, provenance, rights and embedding stay in
ws13_worker.py where they are already correct. Two consequences an operator
has to know:

  * ws13_worker.ocr() reads its extra flags from WS13_OCR_EXTRA_ARGS in the
    WORKER's environment -- the SQS body carries no per-document args -- so
    the winning flags have to be set fleet-wide before the requeue is picked
    up. That variable can hold one arg set at a time, so --requeue refuses a
    batch whose winners disagree unless --requeue-mixed says otherwise.
  * a remedy that REWRITES THE INPUT (qpdf, ghostscript) cannot be requeued
    at all: the worker refetches the original by its content-addressed key
    and checks sha256, so repaired bytes come back 'integrity_mismatch'. A
    repaired document is a new document and goes back through WS12
    admission; it is recorded 'needs_readmit' and left there.

Everything a document ends on is a classified token, never a fragment of
stderr. Commit e9c662a exists because ws13_manifest.error used to hold the
middle of a truncated traceback and was unreadable corpus-wide; safe() and
token() below are what keep that from coming back.

    # the plan, and the exact commands for it (--exec emit is the default)
    ws13_rescue.py --dsn "$WS13_DB_DSN" --all-failed
    # the same plan without the commands, on a node that could run it
    ws13_rescue.py --dsn "$DSN" --all-failed --exec docker
    # run the ladder here, one named document
    ws13_rescue.py --dsn "$DSN" --sha 87473fe10d90 --exec docker --apply

--dry-run is the default and writes nothing anywhere.
"""
from __future__ import annotations

import argparse
import contextlib
import os
import re
import shlex
import subprocess
import sys
import tempfile
from collections import namedtuple

import psycopg

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import ws13_enqueue                                         # noqa: E402
import ws13_reap_stale                                      # noqa: E402

# ocrmypdf's documented exit codes. 0 appears here because ws13_worker.py
# records 'ocr_exit_0' when ocrmypdf reported success and out.pdf was not on
# disk afterwards -- a different defect from any nonzero code.
OCR_EXIT_NAMES = {
    0: 'ok', 1: 'bad_args', 2: 'input_file', 3: 'missing_dependency',
    4: 'invalid_output_pdf', 5: 'file_access_error', 6: 'already_done_ocr',
    7: 'child_process_error', 8: 'encrypted_pdf', 9: 'invalid_config',
    10: 'pdfa_conversion_failed', 15: 'other_error',
}
# Not ocrmypdf's, and mistaking one for an ocrmypdf code sends the ladder
# somewhere useless: docker reports a killed container as 128 + signal, and
# ws13_worker.ocr() returns -1 of its own when the document budget kills the
# container (it converts TimeoutExpired rather than letting it unwind).
CONTAINER_EXIT_NAMES = {
    -1: 'doc_budget_timeout', 137: 'container_oom_killed',
    139: 'container_segfault',
}

# What ws13_worker.ocr() passes on every document. A rescue rung differs from
# the run that failed only by the flags it names, so the comparison is like
# for like; merge_args() is what removes a base flag the rung contradicts.
BASE_ARGS = ('--deskew', '--rotate-pages', '--clean', '--skip-text')
# Same default and same environment variable the worker reads, so a rescue
# on a node runs at the concurrency that node's worker runs at.
OCR_JOBS = os.environ.get('WS13_OCR_JOBS', '2')
# ocrmypdf rejects these combinations with exit 1 BAD_ARGS. Without the
# merge, a --force-ocr rung inherited the worker's --skip-text, exited 1, and
# the ladder recorded a remedy as "failed" when it had never run.
ARG_CONFLICTS = {
    '--force-ocr': ('--skip-text', '--redo-ocr'),
    '--redo-ocr': ('--skip-text', '--force-ocr'),
    '--skip-text': ('--force-ocr', '--redo-ocr'),
}

# Statuses this tool writes. ws13_enqueue.py selects on status, so 'rescued'
# is directly requeueable (`ws13_enqueue.py --status rescued --force`) while
# the two dead ends are not, which is the point: leaving an exhausted
# document at 'error' meant the next --status error sweep retried it forever.
#
# Two things these three do NOT change, both deliberate:
#   * tools/ws13_status.sh groups by status, so all three are visible there;
#     its second query's done/err summary counts the literals 'done' and
#     'error' and will not count them.
#   * ws13_seed.py builds its skip set from status='done' alone, so a
#     re-seed would re-enqueue even a document classified unrescuable. That
#     is unreachable today -- ws13_seed.py takes an S3 conditional-put lock
#     (ws13/fleet/seed.lock) that is never released -- but it is the one
#     path that can undo a terminal classification, and it is a seed-side
#     decision, not one to make by writing 'done' on a document that has no
#     text.
RESCUED_STATUS = 'rescued'
READMIT_STATUS = 'needs_readmit'
TERMINAL_STATUS = 'unrescuable'
# --all-failed means "not 'done' and not already answered by this tool".
SETTLED_STATUSES = ('done', RESCUED_STATUS, READMIT_STATUS, TERMINAL_STATUS)
# ...and, critically, only among statuses this tool can actually reason about.
# 'not done' is NOT that set: the manifest holds 12,519 map_queue rows, which
# are map plates deliberately parked out of the text-OCR path with an
# inventory reason rather than an ocrmypdf exit code, plus 1 inventory_error.
# Selecting on 'not settled' alone matched 12,537 rows against a tool whose
# docstring describes 7, and --apply would have rewritten every parked map
# plate to 'unrescuable' -- a status SETTLED_STATUSES then excludes forever,
# silently retiring 12,519 documents nobody asked this tool to touch.
RESCUABLE_STATUSES = ('error', 'running')
# The ladder reasons about ocrmypdf exit codes, which only ocr_queue
# documents have. born_digital failures are a pypdf text-extraction problem
# and map_plate rows never entered ocrmypdf at all.
RESCUABLE_CLASSES = ('ocr_queue',)

# ws13_manifest.error is TEXT, but an unbounded reason is how the traceback
# fragment got in. One line, bounded, and drawn only from tokens.
REASON_MAX = 300
UNSAFE_CHARS = re.compile(r'[^A-Za-z0-9_:;,.=/+() -]')
# ws13_worker.ocr() slices stderr to [-700:] behind 'ocr_exit_<n>:'.
ERROR_RE = re.compile(r'ocr_exit_(-?\d+)\s*:(.*)$', re.S)
# A rescue container gets the same wall clock one worker document gets
# (WS13_MAX_DOC_SECONDS). Repairs are single-pass rewrites, not OCR.
OCR_SECONDS = int(os.environ.get('WS13_RESCUE_OCR_SECONDS', '3300'))
REPAIR_SECONDS = int(os.environ.get('WS13_RESCUE_REPAIR_SECONDS', '900'))
# A rescue that yields a valid PDF with an empty text layer is not a rescue:
# 3,091 documents are already recorded 'done' with zero extracted characters
# and contribute nothing to retrieval. Do not grow that population.
MIN_RESCUE_CHARS = int(os.environ.get('WS13_RESCUE_MIN_CHARS', '20'))
RESCUER_ID = f'rescue:{os.uname().nodename}'
# Named here rather than read from ws13_worker, which cannot be imported
# without the worker's environment: the emit path has to print the same
# image the docker path would run.
OCR_IMAGE = 'docker.io/jbarlow83/ocrmypdf:latest'

Remedy = namedtuple('Remedy', 'name repair ocr_args why')
Cause = namedtuple('Cause', 'name terminal ladder note')
Classification = namedtuple(
    'Classification', 'sha status exit_code exit_name cause hints ladder')
Attempt = namedtuple('Attempt', 'remedy ok detail')
Row = namedtuple('Row', 'sha256 status s3_key doc_class pages error')

R_PDF_OUTPUT = Remedy(
    'output_type_pdf', None, ('--output-type', 'pdf'),
    'skip PDF/A conversion entirely: the text layer is what WS13 indexes '
    'and PDF/A conformance buys this corpus nothing')
R_PDF_NO_OPTIMIZE = Remedy(
    'output_type_pdf_no_optimize', None,
    ('--output-type', 'pdf', '--optimize', '0', '--max-image-mpixels', '900'),
    'the JPEG optimiser is the other producer of an invalid output PDF -- '
    "e9c662a recovered ff551ca683e6 (16 pages) with --optimize 0 -- and 900 "
    "MP clears PIL's 500 MP bomb limit for the recorded 509 MP plate")
R_FORCE_OCR = Remedy(
    'force_ocr_rasterised', None, ('--output-type', 'pdf', '--force-ocr'),
    'rasterise every page and rebuild the file, so a malformed page object '
    'in the original cannot be copied through into the output')
R_TESS_TIMEOUT = Remedy(
    'tesseract_timeout_1800', None,
    ('--output-type', 'pdf', '--tesseract-timeout', '1800'),
    'a killed tesseract is most often a timed-out one; 1800 s is 3x the '
    "600 s the worker's own strong pass allows and still fits inside "
    'WS13_MAX_DOC_SECONDS (3300 s)')
R_PSM_OSD = Remedy(
    'pagesegmode_1_osd', None,
    ('--output-type', 'pdf', '--tesseract-timeout', '1800',
     '--tesseract-pagesegmode', '1'),
    'psm 1 runs orientation and script detection explicitly instead of '
    'leaving the default segmentation to crash on the page')
R_PSM_SPARSE = Remedy(
    'pagesegmode_11_sparse', None,
    ('--output-type', 'pdf', '--tesseract-timeout', '1800',
     '--tesseract-pagesegmode', '11'),
    'psm 11 reads sparse text with no layout analysis, which is what a map '
    'plate or an annotated drill log actually is')
R_SKIP_BIG = Remedy(
    'skip_oversized_pages', None,
    ('--output-type', 'pdf', '--tesseract-timeout', '1800',
     '--max-image-mpixels', '900', '--skip-big', '200'),
    'lose the offending page, not the document: 200 MP passes every normal '
    'scan and skips only plates like the recorded 52454x9707 (509 MP)')
R_LOW_MEMORY = Remedy(
    'single_job_no_optimize', None,
    ('--output-type', 'pdf', '--optimize', '0', '--jobs', '1',
     '--max-image-mpixels', '900'),
    'one page in flight instead of the worker\'s two: a 137 is the kernel '
    'killing the container, not ocrmypdf reporting anything')
R_QPDF_REPAIR = Remedy(
    'qpdf_decode_repair',
    ('qpdf', ('--decode-level=all', '--object-streams=generate',
              '/work/{in}', '/work/{out}')),
    ('--output-type', 'pdf', '--optimize', '0'),
    'no OCR setting reaches a damaged image stream; qpdf re-encodes every '
    'stream it can decode and rewrites the object structure around the '
    'ones it cannot')
R_GS_REWRITE = Remedy(
    'ghostscript_rewrite',
    ('gs', ('-o', '/work/{out}', '-sDEVICE=pdfwrite', '-dNOPAUSE', '-dBATCH',
            '-dPDFSETTINGS=/prepress', '/work/{in}')),
    ('--output-type', 'pdf', '--force-ocr'),
    'ghostscript re-interprets each page and writes a fresh file, the '
    'heavier repair; --force-ocr because that rewrite can leave a vestigial '
    'text layer which --skip-text would honour, yielding no text again')
R_QPDF_DECRYPT = Remedy(
    'qpdf_decrypt', ('qpdf', ('--decrypt', '/work/{in}', '/work/{out}')),
    ('--output-type', 'pdf'),
    'agency scans are routinely encrypted with an empty owner password, '
    'which qpdf removes without being given one')
R_ROTATE_THRESHOLD = Remedy(
    'rotate_threshold_low', None,
    ('--output-type', 'pdf', '--rotate-pages-threshold', '2',
     '--tesseract-pagesegmode', '1'),
    'the worker already passes --rotate-pages and --deskew, so the only '
    'thing left to change is how sure OSD has to be before it turns a page: '
    "'Too few characters' is OSD declining at the default threshold of 14")

PDFA_LADDER = (R_PDF_OUTPUT, R_PDF_NO_OPTIMIZE, R_FORCE_OCR)
TESSERACT_LADDER = (R_TESS_TIMEOUT, R_PSM_OSD, R_PSM_SPARSE, R_SKIP_BIG)
REPAIR_LADDER = (R_QPDF_REPAIR, R_GS_REWRITE)
GENERIC_LADDER = (R_PDF_OUTPUT, R_PDF_NO_OPTIMIZE, R_QPDF_REPAIR)
# Rungs a matched stderr pattern ADDS to the cause's own ladder.
HINT_LADDERS = {
    'page_geometry': (R_ROTATE_THRESHOLD, R_PSM_SPARSE),
    'oversized_image': (R_SKIP_BIG,),
}

# Ordered: the first pattern that names a cause of its own wins, and
# 'invalid jpeg data' outranks every exit code because a damaged stream is
# not something an OCR flag can reach.
STDERR_PATTERNS = (
    (re.compile(r'invalid jpeg data', re.I), 'damaged_jpeg_stream'),
    (re.compile(r'image file is truncated', re.I), 'damaged_jpeg_stream'),
    (re.compile(r'improbable aspect ratio', re.I), 'page_geometry'),
    (re.compile(r'\bfacing\b', re.I), 'page_geometry'),
    (re.compile(r'too few characters', re.I), 'page_geometry'),
    (re.compile(r'skipping all processing on this page', re.I),
     'page_geometry'),
    (re.compile(r'image too large', re.I), 'oversized_image'),
    (re.compile(r'exceeds limit of \d+ pixels', re.I), 'oversized_image'),
)
# A hint that is a cause in its own right, and therefore replaces the one the
# exit code implies. The rest only extend the ladder.
OVERRIDING_HINTS = ('damaged_jpeg_stream',)

_NODE_NOTE = ('this is a property of the node or of the OCR image, not of '
              'the document: fix that and requeue unchanged with '
              'ws13_enqueue.py')
CAUSES = {
    'invalid_output_pdf': Cause(
        'invalid_output_pdf', 'invalid_output_pdf_unrecoverable',
        PDFA_LADDER,
        'ocrmypdf OCRed the pages and failed its own PDF/A validation'),
    'pdfa_conversion_failed': Cause(
        'pdfa_conversion_failed', 'pdfa_conversion_unrecoverable',
        PDFA_LADDER, 'the PDF/A conversion step itself failed'),
    'child_process_error': Cause(
        'child_process_error', 'child_process_error_unrecoverable',
        TESSERACT_LADDER, 'tesseract crashed or was killed'),
    'damaged_jpeg_stream': Cause(
        'damaged_jpeg_stream', 'damaged_jpeg_stream_unrecoverable',
        REPAIR_LADDER,
        "the input's image stream is damaged, so the input is what has to "
        'be repaired'),
    'input_file_unreadable': Cause(
        'input_file_unreadable', 'input_file_unreadable_unrecoverable',
        REPAIR_LADDER, 'ocrmypdf could not read the input as a PDF'),
    'encrypted_pdf': Cause(
        'encrypted_pdf', 'encrypted_pdf_unrecoverable', (R_QPDF_DECRYPT,),
        'the PDF is encrypted'),
    'no_output_pdf': Cause(
        'no_output_pdf', 'no_output_pdf_unrecoverable',
        (R_PDF_OUTPUT, R_FORCE_OCR),
        'ocrmypdf reported success and left no output file on disk'),
    'other_error': Cause(
        'other_error', 'ocr_other_error_unrecoverable',
        PDFA_LADDER + (R_QPDF_REPAIR,),
        'ocrmypdf reported OTHER_ERROR, which names nothing on its own'),
    'doc_budget_timeout': Cause(
        'doc_budget_timeout', 'doc_budget_timeout_unrecoverable',
        (R_SKIP_BIG, R_LOW_MEMORY),
        "the worker's own budget killed the container; the other lever is "
        'the fleet WS13_DOC_BUDGET_SECONDS, not a document setting'),
    'container_oom_killed': Cause(
        'container_oom_killed', 'container_oom_unrecoverable',
        (R_LOW_MEMORY,), 'the kernel killed the container (128 + SIGKILL)'),
    'container_segfault': Cause(
        'container_segfault', 'container_segfault_unrecoverable',
        (R_QPDF_REPAIR, R_LOW_MEMORY),
        'the container died on SIGSEGV (128 + 11)'),
    # Everything below has NO ladder on purpose: retrying the document is
    # not the fix, and pretending otherwise burns a worker slot per rung.
    'missing_dependency': Cause(
        'missing_dependency', 'missing_dependency_not_a_document_defect', (),
        'a tool is absent from the OCR image; ' + _NODE_NOTE),
    'file_access_error': Cause(
        'file_access_error', 'file_access_error_not_a_document_defect', (),
        'the worker could not read or write its scratch path; ' + _NODE_NOTE),
    'worker_args_invalid': Cause(
        'worker_args_invalid', 'worker_args_invalid_not_a_document_defect',
        (), 'ocrmypdf rejected its own arguments, which come from the '
            'fleet WS13_OCR_EXTRA_ARGS; ' + _NODE_NOTE),
    'already_has_text_layer': Cause(
        'already_has_text_layer', 'already_has_text_layer_reclassify', (),
        'ocrmypdf found an existing text layer; this document belongs in '
        'born_digital, and the reclassification is WS12 admission work'),
    'integrity_mismatch': Cause(
        'integrity_mismatch', 'integrity_mismatch_not_an_ocr_failure', (),
        'the S3 object no longer hashes to its own key, so no OCR setting '
        'is implicated; this is a WS12 re-admission'),
    'born_digital_no_text': Cause(
        'born_digital_no_text', 'born_digital_no_text_reclassify', (),
        'a born_digital document with no extractable text belongs in '
        'ocr_queue; the class change is WS12 admission work'),
    'stale_running_reaped': Cause(
        'stale_running_reaped', 'stale_running_not_diagnosed', (),
        'the row was reaped from a dead worker and was never diagnosed: '
        'requeue it unchanged with ws13_enqueue.py --status error'),
    'unclassified': Cause(
        'unclassified', 'unclassified_error_not_an_ocr_exit', (),
        'the recorded error carries no ocr_exit_<n>, so there is nothing to '
        'classify; read it by hand before spending a worker on it'),
}
# Exit code -> cause name. Codes absent here get a dynamic unknown_exit_<n>.
EXIT_CAUSES = {
    0: 'no_output_pdf', 1: 'worker_args_invalid', 2: 'input_file_unreadable',
    3: 'missing_dependency', 4: 'invalid_output_pdf',
    5: 'file_access_error', 6: 'already_has_text_layer',
    7: 'child_process_error', 8: 'encrypted_pdf', 9: 'worker_args_invalid',
    10: 'pdfa_conversion_failed', 15: 'other_error',
    -1: 'doc_budget_timeout', 137: 'container_oom_killed',
    139: 'container_segfault',
}

SELECT_ROWS = """
SELECT m.sha256, m.status, m.s3_key, m.doc_class, m.pages, m.error
  FROM ws13_manifest m
 WHERE {where}
 ORDER BY m.sha256
"""
# Compare-and-set on the status that was read, for the reason
# ws13_reap_stale.py documents: this connection is autocommit, so a row lock
# would be released before the next statement runs. If a worker picked the
# document up between the SELECT and this UPDATE, the write misses and says
# so rather than overwriting a live row.
RECORD_ONE = """
UPDATE ws13_manifest
   SET status = %s, error = %s, worker_id = %s, updated_at = now()
 WHERE sha256 = %s
   AND status = %s
"""


def token(text, limit=48):
    """Reduce raw text to a greppable slug.

    Nothing that came from stderr reaches ws13_manifest.error any other way.
    e9c662a is the reason: the column used to hold the middle of a truncated
    traceback, so 'the stated reason' was unmet corpus-wide.
    """
    slug = re.sub(r'[^a-z0-9]+', '_', (text or '').lower()).strip('_')
    return slug[:limit] or 'unknown'


def safe(reason):
    """One line, bounded, and drawn from the whitelist. The last guard."""
    collapsed = ' '.join((reason or '').split())
    return UNSAFE_CHARS.sub('_', collapsed)[:REASON_MAX] or 'unknown'


def parse_error(error):
    """(exit_code, stderr_tail) from a ws13_manifest.error string.

    Returns (None, text) for an error that is not an OCR exit at all --
    'integrity_mismatch', 'born_digital_no_extractable_text', a reap reason.
    Those are classified by text below, not by code.
    """
    match = ERROR_RE.search(error or '')
    if not match:
        return None, (error or '')
    return int(match.group(1)), match.group(2)


def stderr_hints(tail):
    """Hint names matched in the stderr tail, in pattern order, deduped."""
    hints = []
    for pattern, name in STDERR_PATTERNS:
        if name not in hints and pattern.search(tail or ''):
            hints.append(name)
    return tuple(hints)


def cause_for(exit_code, tail):
    """The Cause an error resolves to, before hints extend its ladder."""
    if exit_code is None:
        text = (tail or '').strip()
        if text.startswith('integrity_mismatch'):
            return CAUSES['integrity_mismatch']
        if text.startswith('born_digital_no_extractable_text'):
            return CAUSES['born_digital_no_text']
        if text.startswith('stale_running_reaped'):
            return CAUSES['stale_running_reaped']
        return CAUSES['unclassified']
    name = EXIT_CAUSES.get(exit_code)
    if name:
        return CAUSES[name]
    # An undocumented code is still a fact: name it with its number so the
    # manifest reason stays greppable, and give it the generic ladder rather
    # than refusing to try anything. Negatives are spelled 'neg9', not '-9':
    # subprocess reports a signal-killed child as a negative code, and a
    # hyphen would put a character in the terminal token that every other
    # token in this file is free of.
    label = (str(exit_code) if exit_code >= 0
             else f'neg{abs(exit_code)}')
    return Cause(f'unknown_exit_{label}',
                 f'unknown_exit_{label}_unrecoverable', GENERIC_LADDER,
                 f'exit {exit_code} is not a documented ocrmypdf code')


def dedupe(remedies):
    """Preserve ladder order, drop a rung a hint would repeat."""
    seen, out = set(), []
    for remedy in remedies:
        if remedy.name not in seen:
            seen.add(remedy.name)
            out.append(remedy)
    return tuple(out)


def ladder_for(cause, hints):
    """The cause's ladder, extended by the rungs its stderr hints add.

    A hint never CREATES a ladder: 'improbable aspect ratio' on an exit 3
    MISSING_DEPENDENCY is a page warning printed on the way to a node defect,
    and turning it into three retries of a document that cannot succeed
    spends a worker slot per rung to learn nothing.
    """
    ladder = list(cause.ladder)
    if not ladder:
        return ()
    for hint in hints:
        ladder.extend(HINT_LADDERS.get(hint, ()))
    return dedupe(ladder)


def classify(sha, status, error):
    """Everything decided about one document before anything is run."""
    exit_code, tail = parse_error(error)
    hints = stderr_hints(tail)
    cause = cause_for(exit_code, tail)
    for hint in hints:
        if hint in OVERRIDING_HINTS:
            # 'invalid jpeg data reading stream' beats the exit code: the
            # input is damaged, so the exit code only records which stage
            # noticed.
            cause = CAUSES[hint]
            break
    exit_name = OCR_EXIT_NAMES.get(exit_code) or \
        CONTAINER_EXIT_NAMES.get(exit_code)
    if exit_code is not None and not exit_name:
        exit_name = f'undocumented_{exit_code}'
    return Classification(sha=sha, status=status, exit_code=exit_code,
                          exit_name=exit_name, cause=cause, hints=hints,
                          ladder=ladder_for(cause, hints))


def merge_args(base, extra):
    """base flags, minus anything `extra` sets or contradicts, plus extra.

    ocrmypdf exits 1 BAD_ARGS on --force-ocr together with the worker's
    --skip-text, and a rung that never ran would otherwise be recorded as a
    remedy that failed -- the ladder would escalate past a fix that was
    never tried.
    """
    drop = set()
    for flag in extra:
        if flag.startswith('--'):
            drop.add(flag)
            drop.update(ARG_CONFLICTS.get(flag, ()))
    out, skip_value = [], False
    for flag in base:
        if skip_value and not flag.startswith('--'):
            continue
        skip_value = flag in drop
        if not skip_value:
            out.append(flag)
    return tuple(out) + tuple(extra)


def ocr_argv(remedy, in_name='in.pdf', out_name='out.pdf',
             sidecar='out.txt', jobs=OCR_JOBS):
    """The ocrmypdf argv one rung runs, shaped like ws13_worker.ocr().

    --sidecar and --jobs go through merge_args with everything else, not
    onto the end afterwards. Appended, they came AFTER the rung's own flags,
    and ocrmypdf's argparse takes the last value: the single_job_no_optimize
    rung asked for --jobs 1 and then handed itself --jobs 2, so the one
    remedy for a container the kernel killed ran at exactly the concurrency
    that got it killed.
    """
    base = tuple(BASE_ARGS) + ('--sidecar', f'/work/{sidecar}',
                               '--jobs', str(jobs))
    args = list(merge_args(base, remedy.ocr_args))
    args += [f'/work/{in_name}', f'/work/{out_name}']
    return args


def repair_argv(remedy, in_name='in.pdf', out_name='repaired.pdf'):
    """(entrypoint, argv) for a rung that rewrites the input, else None."""
    if not remedy.repair:
        return None
    tool, template = remedy.repair
    return tool, [part.format(**{'in': in_name, 'out': out_name})
                  for part in template]


def ocr_input_name(remedy):
    """The file a rung's ocrmypdf actually reads.

    One rule in one place: the plan, the emitted commands and the docker
    executor all have to agree, and a plan that says it will OCR in.pdf
    while the command OCRs repaired.pdf is a plan an operator cannot check.
    """
    return 'repaired.pdf' if remedy.repair else 'in.pdf'


def docker_command(work, argv, entrypoint=None):
    """The literal `docker run` an operator would paste. Mirrors
    ws13_worker.docker(), minus the --name/force-remove bookkeeping that
    only matters to a process that can be interrupted."""
    cmd = ['docker', 'run', '--rm', '--user', '0:0', '-v', f'{work}:/work']
    if entrypoint:
        cmd += ['--entrypoint', entrypoint]
    return cmd + [OCR_IMAGE] + list(argv)


def sidecar_chars(path):
    """Non-whitespace characters in an ocrmypdf sidecar, 0 if absent."""
    if not os.path.exists(path):
        return 0
    with open(path, encoding='utf-8', errors='replace') as handle:
        return len(''.join(handle.read().split()))


class EmitExecutor:
    """Prints the exact commands for an operator. Runs nothing, ever."""

    runs = False

    def __init__(self, args):
        self.work = args.work_dir

    def work_for(self, doc):
        return os.path.join(self.work, doc.sha256)

    def document(self, doc):
        return contextlib.nullcontext(self.work_for(doc))

    def setup(self, doc):
        """Fetch the original once per document, not once per rung."""
        work = self.work_for(doc)
        return [f'mkdir -p {shlex.quote(work)}',
                f'aws s3 cp s3://$WS13_BUCKET/{doc.s3_key} '
                f'{shlex.quote(os.path.join(work, "in.pdf"))}']

    def commands(self, doc, remedy):
        work = self.work_for(doc)
        lines = []
        repair = repair_argv(remedy)
        if repair:
            entrypoint, argv = repair
            lines.append(' '.join(shlex.quote(part) for part in
                                  docker_command(work, argv, entrypoint)))
        argv = ocr_argv(remedy, in_name=ocr_input_name(remedy))
        lines.append(' '.join(shlex.quote(part) for part in
                              docker_command(work, argv)))
        return lines

    def attempt(self, doc, work, remedy):
        raise RuntimeError('emit mode runs nothing; use --exec docker')


class DockerExecutor:
    """Runs the ladder here, on a node that has docker and the OCR image."""

    runs = True

    def __init__(self, args):
        # ws13_worker reads WS13_BUCKET / WS13_QUEUE_URL / WS13_DB_DSN at
        # import time and builds three boto3 clients there, so it is
        # imported on this path only: an operator running --exec emit from a
        # laptop has none of that and needs none of it.
        import ws13_worker
        self.worker = ws13_worker
        self.args = args

    @contextlib.contextmanager
    def document(self, doc):
        with tempfile.TemporaryDirectory(dir=self.args.work_dir) as work:
            # The container runs --user 0:0 against a bind mount, the same
            # arrangement ws13_worker.process() sets up.
            os.chmod(work, 0o777)
            raw = self.worker.s3.get_object(
                Bucket=self.worker.BUCKET, Key=doc.s3_key)['Body'].read()
            path = os.path.join(work, 'in.pdf')
            with open(path, 'wb') as handle:
                handle.write(raw)
            os.chmod(path, 0o644)
            yield work

    def _run(self, work, argv, timeout, entrypoint=None):
        try:
            return self.worker.docker(argv, work, timeout=timeout,
                                      entrypoint=entrypoint)
        except subprocess.TimeoutExpired:
            return None

    def attempt(self, doc, work, remedy):
        # A rung inherits nothing from the rung before it. A stale out.txt
        # would be measured as this rung's text, and a stale repaired.pdf
        # from an earlier repair would be OCRed as this rung's repair.
        # Cleared before the repair runs, because the repair writes one of
        # these files itself.
        for name in ('out.pdf', 'out.txt', 'repaired.pdf'):
            path = os.path.join(work, name)
            if os.path.exists(path):
                os.unlink(path)
        repair = repair_argv(remedy)
        if repair:
            entrypoint, argv = repair
            result = self._run(work, argv, REPAIR_SECONDS, entrypoint)
            if result is None:
                return Attempt(remedy, False, 'repair_timeout')
            if result.returncode != 0:
                return Attempt(remedy, False,
                               f'repair_exit_{result.returncode}')
            if not os.path.exists(os.path.join(work, 'repaired.pdf')):
                # qpdf and gs both report success on some inputs they did
                # not actually write; OCRing the original here would record
                # the repair as the remedy that worked.
                return Attempt(remedy, False, 'repair_produced_nothing')
        result = self._run(
            work, ocr_argv(remedy, in_name=ocr_input_name(remedy)),
            OCR_SECONDS)
        if result is None:
            return Attempt(remedy, False, 'ocr_timeout')
        if result.returncode != 0:
            # The exit code is the fact worth keeping; the stderr tail goes
            # to stdout for the operator and never into the manifest.
            print(f'      stderr: {safe(result.stderr[-300:])}')
            return Attempt(remedy, False, f'ocr_exit_{result.returncode}')
        if not os.path.exists(os.path.join(work, 'out.pdf')):
            return Attempt(remedy, False, 'no_output_pdf')
        chars = sidecar_chars(os.path.join(work, 'out.txt'))
        if chars < self.args.min_chars:
            return Attempt(remedy, False, f'empty_text_{chars}_chars')
        return Attempt(remedy, True, f'text_{chars}_chars')


def run_ladder(executor, doc, ladder, echo=print):
    """Try each rung in order, stop at the first that produces text.

    Escalation is failure-driven and one-directional: a rung is only reached
    because every rung before it failed, and the attempt trail records which
    ones and why, so an exhausted document says what was tried rather than
    'retried 4 times'.
    """
    attempts = []
    with executor.document(doc) as work:
        for position, remedy in enumerate(ladder, 1):
            echo(f'    rung {position}/{len(ladder)} {remedy.name}')
            attempt = executor.attempt(doc, work, remedy)
            attempts.append(attempt)
            echo(f'      {"OK" if attempt.ok else "failed"}: '
                 f'{attempt.detail}')
            if attempt.ok:
                return attempts, remedy
    return attempts, None


def trail(attempts):
    """'name=detail,name=detail' -- what was tried and what happened."""
    return ','.join(f'{a.remedy.name}={token(a.detail)}' for a in attempts)


def success_reason(remedy, attempts):
    return safe(f'rescue_ok:{remedy.name}: after {len(attempts)} attempt(s) '
                f'({trail(attempts)}); ocr_args '
                f'{" ".join(remedy.ocr_args)}')


def readmit_reason(remedy, attempts):
    return safe(f'rescue_repaired:{remedy.name}: after {len(attempts)} '
                f'attempt(s) ({trail(attempts)}); input rewritten, so the '
                f'sha256 changes and WS12 must re-admit it')


def terminal_reason(cause, attempts):
    """The classified dead end, e.g. damaged_jpeg_stream_unrecoverable.

    Never a traceback fragment and never a stderr slice: the token comes
    from CAUSES and the trail from remedy names and classified details.
    """
    if not attempts:
        return safe(f'rescue_none:{cause.terminal}: no remedy applies '
                    f'({cause.name})')
    return safe(f'rescue_exhausted:{cause.terminal}: {len(attempts)} '
                f'remedies tried ({trail(attempts)})')


def sha_selector(value):
    """ws13_reap_stale's hex validation, reused rather than rewritten.

    Same reason it exists there: the value becomes a LIKE pattern, so an
    unset "$SHA" would widen the selector to every row instead of narrowing
    it to one.
    """
    return ws13_reap_stale.sha_selector(value)


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--dsn', default=os.environ.get('WS13_DB_DSN'))
    p.add_argument('--queue-url', default=os.environ.get('WS13_QUEUE_URL'),
                   help='passed through to ws13_enqueue.py on --requeue')
    p.add_argument('--sha', action='append', default=[], type=sha_selector,
                   help='explicit sha256 (repeatable; full or hex prefix)')
    p.add_argument('--status', action='append', default=[],
                   help='manifest status to select (repeatable)')
    p.add_argument('--cls', action='append', default=list(RESCUABLE_CLASSES),
                   help=f'restrict to these doc_class values (repeatable; '
                        f'default {", ".join(RESCUABLE_CLASSES)}). map_plate '
                        f'rows never entered ocrmypdf and have no exit code '
                        f'for the ladder to classify')
    p.add_argument('--rescuable-statuses', action='append', default=None,
                   help=f'statuses --all-failed may select (repeatable; '
                        f'default {", ".join(RESCUABLE_STATUSES)})')
    p.add_argument('--fleet-args-set', action='store_true',
                   help='confirm the fleet already carries the winning '
                        'WS13_OCR_EXTRA_ARGS; without it --requeue prints '
                        'the commands and sends nothing, because workers '
                        'long-poll and would consume the message under the '
                        'old environment')
    p.add_argument('--all-failed', action='store_true',
                   help='every row that is not done and not already settled '
                        'by this tool (the 7 documents, today)')
    p.add_argument('--include-settled', action='store_true',
                   help=f'let --all-failed reselect rows already at '
                        f'{", ".join(SETTLED_STATUSES[1:])}')
    p.add_argument('--limit', type=int, help='cap the documents selected')
    p.add_argument('--exec', dest='exec_mode', choices=('emit', 'docker'),
                   default='emit',
                   help='emit: print the exact commands for an operator '
                        '(default). docker: run the ladder here, on a node '
                        'with docker and the OCR image')
    p.add_argument('--apply', action='store_true',
                   help='run the ladder and write the outcome (requires '
                        '--exec docker)')
    p.add_argument('--dry-run', action='store_true',
                   help='plan only, write nothing (the default)')
    p.add_argument('--requeue', action='store_true',
                   help='hand rescued documents to ws13_enqueue.py')
    p.add_argument('--requeue-mixed', action='store_true',
                   help='requeue winners with different ocr_args in one '
                        'call, accepting that WS13_OCR_EXTRA_ARGS can only '
                        'carry one of them')
    p.add_argument('--enqueue-arg', action='append', default=[],
                   help='extra argument forwarded verbatim to '
                        'ws13_enqueue.py (repeatable). A forwarded FLAG has '
                        "to use the '=' form -- argparse reads a bare "
                        '"--harvest-manifest" as an option of this tool, '
                        'not as this option\'s value: '
                        '--enqueue-arg=--harvest-manifest '
                        '--enqueue-arg var/ws12/manifest.jsonl')
    p.add_argument('--reap-running', action='store_true',
                   help="hand status='running' rows to ws13_reap_stale.py "
                        'instead of only reporting them')
    p.add_argument('--older-than-hours', type=float,
                   default=ws13_reap_stale.DEFAULT_HOURS,
                   help='forwarded to ws13_reap_stale.py')
    p.add_argument('--min-chars', type=int, default=MIN_RESCUE_CHARS,
                   help=f'non-whitespace characters a rescue must produce '
                        f'to count (default {MIN_RESCUE_CHARS})')
    p.add_argument('--work-dir', default=os.environ.get(
        'WS13_SCRATCH', '/opt/ws13/scratch'),
        help='scratch directory for the rescue containers')
    args = p.parse_args(argv)
    if args.rescuable_statuses is None:
        args.rescuable_statuses = list(RESCUABLE_STATUSES)
    return args


def select(conn, args):
    """Rows to rescue. Refuses to run with no selector.

    --cls is a NARROWING default, not a selector: it defaults to ocr_queue,
    so counting it as one would mean a bare invocation quietly selected every
    OCR document in the corpus instead of refusing.
    """
    where, params = [], []
    selected = False
    if args.status:
        selected = True
        where.append('m.status = ANY(%s)')
        params.append(args.status)
    if args.sha:
        selected = True
        # Prefixes, so the 12-char ids the status tooling prints paste in.
        where.append('(m.sha256 = ANY(%s) OR ' +
                     ' OR '.join(['m.sha256 LIKE %s'] * len(args.sha)) + ')')
        params.append(args.sha)
        params.extend(s + '%' for s in args.sha)
    if args.all_failed:
        selected = True
        # Bounded on BOTH axes. 'not settled' alone is not a population this
        # tool can classify -- see RESCUABLE_STATUSES above.
        where.append('m.status = ANY(%s)')
        params.append(list(args.rescuable_statuses))
        if not args.include_settled:
            where.append('NOT (m.status = ANY(%s))')
            params.append([s for s in SETTLED_STATUSES if s != 'done'])
    if args.cls:
        where.append('m.doc_class = ANY(%s)')
        params.append(list(args.cls))
    if not selected:
        sys.exit('refusing to run with no selector: pass --sha, --status or '
                 '--all-failed')
    sql = SELECT_ROWS.format(where=' AND '.join(where))
    rows = [Row._make(r) for r in conn.execute(sql, params).fetchall()]
    return rows[:args.limit] if args.limit else rows


def print_plan(cls_, doc, emitter=None):
    """Per document: the recorded exit code, the classified cause, and the
    exact ladder that would be attempted -- so an operator reads the plan
    before anything runs. `emitter` adds the literal commands for --exec
    emit; it is the whole difference between the two execution modes.
    """
    exit_text = ('none recorded' if cls_.exit_code is None
                 else f'{cls_.exit_code} {cls_.exit_name}')
    print(f'  {cls_.sha[:12]} status={cls_.status} exit={exit_text} '
          f'cause={cls_.cause.name}')
    print(f'    {cls_.cause.note}')
    if cls_.hints:
        print(f'    stderr tail matched: {", ".join(cls_.hints)}')
    if not cls_.ladder:
        print('    no remedy ladder: retrying this document is not the fix')
        print(f'    would be recorded {TERMINAL_STATUS}: '
              f'{terminal_reason(cls_.cause, [])}')
        return
    if emitter is not None:
        for line in emitter.setup(doc):
            print(f'    $ {line}')
    for position, remedy in enumerate(cls_.ladder, 1):
        print(f'    rung {position}/{len(cls_.ladder)} {remedy.name}')
        print(f'      why: {remedy.why}')
        if emitter is None:
            # The docker path shows the argv; the emit path shows the whole
            # command, which already contains it, so it is not printed twice.
            repair = repair_argv(remedy)
            if repair:
                print(f'      repair: {repair[0]} {" ".join(repair[1])}')
            print('      ocrmypdf: '
                  + ' '.join(ocr_argv(remedy, ocr_input_name(remedy))))
        else:
            for line in emitter.commands(doc, remedy):
                print(f'      $ {line}')
    print(f'    all rungs failing is recorded {TERMINAL_STATUS}: '
          f'{cls_.cause.terminal}')


def record(conn, doc, status, reason):
    """Compare-and-set the outcome onto the row that was read."""
    return conn.execute(RECORD_ONE, (status, safe(reason), RESCUER_ID,
                                     doc.sha256, doc.status)).rowcount


def handle_running(args, rows):
    """Defer status='running' to ws13_reap_stale.py, never duplicate it.

    5c991bfa4e90 is stuck here: a worker was killed mid-document, so the row
    says 'running' forever and its error column predates the kill. There is
    nothing to classify until it has been reaped into 'error', and the
    reaping logic -- the live-worker check, the age floor, the
    compare-and-set -- already exists and is tested.
    """
    for doc in rows:
        print(f"  {doc.sha256[:12]} is status='running': not classifiable "
              f'until it is reaped')
    if not args.reap_running:
        print('  defer to: ws13_reap_stale.py --dsn "$WS13_DB_DSN" '
              + ' '.join(f'--sha {d.sha256[:12]}' for d in rows) + ' --apply')
        return 0
    argv = ['--dsn', args.dsn, '--older-than-hours',
            str(args.older_than_hours)]
    for doc in rows:
        argv += ['--sha', doc.sha256]
    if args.apply:
        argv.append('--apply')
    print(f'  ws13_reap_stale.py {" ".join(argv)}')
    return ws13_reap_stale.main(argv)


def requeue(args, winners):
    """Return rescued documents to the ONE indexing path, ws13_enqueue.py.

    Grouped by winning ocr_args because ws13_worker.ocr() reads them from
    WS13_OCR_EXTRA_ARGS in the worker's environment and the SQS body carries
    no per-document arguments: one fleet-wide value, so one group per run.
    """
    groups = {}
    for doc, remedy in winners:
        groups.setdefault(remedy.ocr_args, []).append(doc)
    if len(groups) > 1 and not args.requeue_mixed:
        print('refusing to requeue winners with different ocr_args in one '
              'call: WS13_OCR_EXTRA_ARGS is fleet-wide and can carry one '
              'set at a time. Run one group per fleet setting, or pass '
              '--requeue-mixed to accept it.', file=sys.stderr)
        for ocr_args, docs in groups.items():
            print(f'  {" ".join(ocr_args)}: '
                  f'{", ".join(d.sha256[:12] for d in docs)}',
                  file=sys.stderr)
        return 1
    # A winner the worker cannot actually express. ws13_worker.ocr() appends
    # WS13_OCR_EXTRA_ARGS to its own BASE_ARGS with no merge, so a rung that
    # had to DROP one of those flags to work here would arrive alongside it
    # there: --force-ocr next to the worker's --skip-text is exit 1 BAD_ARGS,
    # and the variable is fleet-wide, so it would fail not just this document
    # but every document any worker touched while it was set.
    unexpressible = {}
    for ocr_args in list(groups):
        dropped = set(BASE_ARGS) - set(merge_args(BASE_ARGS, ocr_args))
        if dropped:
            unexpressible[ocr_args] = sorted(dropped)
    if unexpressible:
        print('refusing to requeue: these winning arguments contradict the '
              'worker\'s own base flags, and WS13_OCR_EXTRA_ARGS is appended '
              'to them rather than merged, so ocrmypdf would exit 1 BAD_ARGS '
              'for every document the fleet touched:', file=sys.stderr)
        for ocr_args, dropped in unexpressible.items():
            docs = groups[ocr_args]
            print(f'  {" ".join(ocr_args)} would have to drop '
                  f'{" ".join(dropped)}: '
                  f'{", ".join(d.sha256[:12] for d in docs)}', file=sys.stderr)
        print('  Re-OCR these by hand with the printed remedy, or teach '
              'ws13_worker.ocr() to merge_args() before this can be '
              'automated.', file=sys.stderr)
        return 1
    failures = 0
    for ocr_args, docs in groups.items():
        setting = shlex.quote(' '.join(ocr_args))
        argv = ['--dsn', args.dsn, '--force']
        if args.queue_url:
            argv += ['--queue-url', args.queue_url]
        for doc in docs:
            argv += ['--sha', doc.sha256]
        argv += args.enqueue_arg
        print(f'set WS13_OCR_EXTRA_ARGS={setting} on the fleet FIRST')
        print(f'ws13_enqueue.py {" ".join(argv)}')
        if not args.fleet_args_set:
            # Sending now would be worse than not sending: workers long-poll,
            # so the message is picked up seconds later with the OLD
            # environment, the original failure reproduces, and
            # ws13_worker.set_status overwrites the row's rescued status and
            # the reason that recorded how it was rescued.
            print('  not sent: pass --fleet-args-set once the fleet actually '
                  'carries that value', file=sys.stderr)
            failures += 1
            continue
        failures += 1 if ws13_enqueue.main(argv) else 0
    return failures


def main(argv=None):
    args = parse_args(argv)
    if not args.dsn:
        sys.exit('need --dsn (or WS13_DB_DSN)')
    if args.apply and args.dry_run:
        sys.exit('--apply and --dry-run are mutually exclusive')
    if args.apply and args.exec_mode != 'docker':
        # emit mode never learns whether a rung worked, so it must not be
        # allowed to write a terminal state that claims it did.
        sys.exit('--apply requires --exec docker: emit mode runs nothing, '
                 'so it cannot know what happened')
    if args.requeue and not args.apply:
        sys.exit('--requeue requires --apply: there is nothing to requeue '
                 'until a remedy has actually been proven')

    conn = psycopg.connect(args.dsn, autocommit=True)
    rows = select(conn, args)
    if not rows:
        print('nothing matched the selector')
        return 0

    running = [r for r in rows if r.status == 'running']
    rows = [r for r in rows if r.status != 'running']
    reap_code = 0
    if running:
        print(f'{len(running)} row(s) at status=\'running\'')
        reap_code = handle_running(args, running)

    if not rows:
        return reap_code

    emitter = EmitExecutor(args) if args.exec_mode == 'emit' else None
    plans = [(doc, classify(doc.sha256, doc.status, doc.error))
             for doc in rows]
    print(f'{len(plans)} document(s) to rescue')
    for doc, cls_ in plans:
        print_plan(cls_, doc, emitter)

    if not args.apply:
        print('dry run: nothing was run and nothing was written. Pass '
              '--exec docker --apply on a worker node to run the ladder.')
        return reap_code
    executor = DockerExecutor(args)

    rescued, readmit, exhausted, missed = [], 0, 0, 0
    for doc, cls_ in plans:
        print(f'  {doc.sha256[:12]} {cls_.cause.name}')
        attempts, winner = run_ladder(executor, doc, cls_.ladder)
        if winner is None:
            status = TERMINAL_STATUS
            reason = terminal_reason(cls_.cause, attempts)
        elif winner.repair:
            status = READMIT_STATUS
            reason = readmit_reason(winner, attempts)
        else:
            status = RESCUED_STATUS
            reason = success_reason(winner, attempts)
        # Counted only once the write lands, so the closing summary and the
        # manifest agree: a document whose row moved under us is reported as
        # not recorded and nothing else, never as both rescued and missed.
        if not record(conn, doc, status, reason):
            missed += 1
            print(f'    NOT recorded: the row moved off {doc.status!r} '
                  f'between the read and the write, so a worker owns it')
            continue
        print(f'    recorded {status}: {reason}')
        if status == RESCUED_STATUS:
            rescued.append((doc, winner))
        elif status == READMIT_STATUS:
            readmit += 1
        else:
            exhausted += 1

    print(f'rescued {len(rescued)}, needs_readmit {readmit}, '
          f'unrescuable {exhausted}, not recorded {missed}')
    requeue_code = 0
    if rescued and args.requeue:
        requeue_code = requeue(args, rescued)
    elif rescued:
        print('requeue them with: ws13_rescue.py ... --requeue, or '
              'ws13_enqueue.py --status rescued --force')
    # An exhausted document is a documented dead end, not a crash, but the
    # run must not read as clean.
    return 1 if (exhausted or missed or requeue_code or reap_code) else 0


if __name__ == '__main__':
    raise SystemExit(main())
