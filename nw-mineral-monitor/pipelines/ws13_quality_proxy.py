#!/usr/bin/env python3
"""Per-page lexical quality proxy for the WS13 corpus.

True tesseract confidences were lost to a silent render failure in the first
bulk sweep (pdftoppm is not on PATH in the ocrmypdf image, so every page
render returned rc=127 and every confidence is NULL). Rather than re-render
~350k pages, score each page's OCR text lexically -- alpha ratio,
dictionary-shaped tokens, run-length noise -- as an honest, clearly-labeled
proxy stored as quality_score/quality_method. Pages scoring weak get
low_confidence=true and form the escalation set for true TSV measurement.

Claim-once semantics. The previous revision selected on `quality_score IS
NULL`, but score() deliberately returns None for a page under 40 characters
("unknown, not zero") and the UPDATE wrote that None straight back -- so
those rows re-qualified for their own work queue on every pass. With no
ORDER BY, the same 400 unscoreable rows came back forever: 147.6M UPDATEs
over 61.7 hours, and 73,614 perfectly scoreable pages sitting behind the
roadblock that were never reached at all. The work predicate is now
`quality_method IS NULL`, which every processed row gets exactly once
whether or not it could be scored, so the sweep advances and terminates.
NULL score still means "unknown" -- that semantic was right and is kept.
"""
import os, re, sys, psycopg

DSN = os.environ.get('WS13_DB_DSN')
if not DSN:
    DSN = ("postgresql://nwmm:%s@nwmm-ws13.cdso6e0me8he.us-west-2.rds."
           "amazonaws.com:5432/nwmm?sslmode=require" % os.environ['PW'])
BATCH = 400
WEAK = 55.0

conn = psycopg.connect(DSN, autocommit=True)
conn.execute("ALTER TABLE ws13_pages ADD COLUMN IF NOT EXISTS quality_score REAL")
conn.execute("ALTER TABLE ws13_pages ADD COLUMN IF NOT EXISTS quality_method TEXT")

WORD = re.compile(r"[A-Za-z]{2,}")
VOWEL = re.compile(r"[aeiouAEIOU]")


def score(text):
    if not text or len(text) < 40:
        return None  # blank/near-blank page: unknown, not zero
    n = len(text)
    alpha = sum(c.isalpha() for c in text) / n
    words = WORD.findall(text)
    if not words:
        return 5.0
    vowelly = sum(1 for w in words if VOWEL.search(w)) / len(words)
    wlen = sum(len(w) for w in words) / len(words)
    len_ok = 1.0 if 3.0 <= wlen <= 9.0 else 0.5
    junk = len(re.findall(r"[^\w\s.,;:()'\"/%$#&-]", text)) / n
    return round(100 * max(0.0, min(1.0,
        0.45 * alpha + 0.35 * vowelly + 0.10 * len_ok + 0.10 * (1 - min(1.0, junk * 12)))), 1)


def log(msg):
    print(msg, flush=True)


# One-time repair. Rows the stuck loop processed carry method
# 'lexical-proxy-v1' with a NULL score, which reads as "scored" when it was
# not. They are near-blank (1-39 chars of chunk text); label them honestly so
# the coverage numbers mean what they say.
repaired = conn.execute(
    "UPDATE ws13_pages SET quality_method='lexical-proxy-v1-blank' "
    "WHERE quality_score IS NULL AND quality_method='lexical-proxy-v1'").rowcount
if repaired:
    log(f'repaired {repaired} mislabeled near-blank rows')

# Without this the work predicate degrades to a table scan per batch, and the
# scan lengthens as the remaining NULLs get sparser. CONCURRENTLY so nothing
# writing ws13_pages is blocked.
log('building work index')
conn.execute("CREATE INDEX CONCURRENTLY IF NOT EXISTS ws13_pages_qm_null "
             "ON ws13_pages (sha256, page) WHERE quality_method IS NULL")

updated = 0
while True:
    rows = conn.execute(
        """SELECT p.sha256, p.page FROM ws13_pages p
           WHERE p.quality_method IS NULL AND p.chars > 0 LIMIT %s""",
        (BATCH,)).fetchall()
    if not rows:
        break
    claimed = 0
    for sha, page in rows:
        texts = [t for (t,) in conn.execute(
            "SELECT text FROM ws13_chunks WHERE sha256=%s AND page=%s "
            "ORDER BY ordinal", (sha, page))]
        s = score(' '.join(texts))
        claimed += conn.execute(
            "UPDATE ws13_pages SET quality_score=%s::real, "
            "quality_method=CASE WHEN %s::real IS NULL "
            "  THEN 'lexical-proxy-v1-blank' ELSE 'lexical-proxy-v1' END, "
            "low_confidence=(%s::real IS NOT NULL AND %s::real < %s) "
            "WHERE sha256=%s AND page=%s",
            (s, s, s, s, WEAK, sha, page)).rowcount
        updated += 1
    if claimed == 0:
        # Every row in the batch failed to take a method. Rather than spin,
        # stop loudly -- this is the failure mode that burned 61.7 hours.
        log(f'ABORT: batch of {len(rows)} claimed nothing; predicate is not '
            f'advancing. {updated} scored before this point.')
        sys.exit(1)
    if updated % 20000 < BATCH:
        log(f'scored {updated}')

# Pages with chars = 0 were never eligible for the loop. Account for them
# explicitly rather than leaving them indistinguishable from unprocessed work.
blank = conn.execute(
    "UPDATE ws13_pages SET quality_method='lexical-proxy-v1-blank' "
    "WHERE quality_method IS NULL").rowcount
log(f'marked {blank} zero-char pages blank')

conn.execute("DROP INDEX IF EXISTS ws13_pages_qm_null")

total, scored, weak, unknown = conn.execute(
    "SELECT COUNT(*), COUNT(quality_score), "
    "COUNT(*) FILTER (WHERE low_confidence), "
    "COUNT(*) FILTER (WHERE quality_score IS NULL) FROM ws13_pages").fetchone()
log(f'FINAL pages {total}, scored {scored}, weak {weak}, unknown {unknown}')
if total != scored + unknown:
    log('WARNING: scored + unknown does not equal total')
