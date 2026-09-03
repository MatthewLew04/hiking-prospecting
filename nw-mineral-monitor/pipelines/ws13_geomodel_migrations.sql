-- WS13 geomodel driver ledger: one row per (document, mine) the sharded
-- corpus driver (pipelines/ws13_geomodel.py) has decided about.
--
-- Why a table and not a file: 64 processes on one c7g.16xlarge each own a
-- shard of the 56,282 documents and must be able to answer "is this
-- (document, mine) already published at this content hash?" without
-- re-reading each other's files, and an operator must be able to ask
-- "parked by reason" over the whole corpus with one GROUP BY. The ledger is
-- what makes a rerun a no-op: a row whose status is 'published' and whose
-- content_hash (carved-text hash + parser + builder + publisher + driver
-- version + answer policy + context flag) is unchanged is skipped, and a
-- document that errored is retried at most WS13_GEOMODEL_MAX_ATTEMPTS times.
--
-- Applied by `ws13_geomodel.py --migrate`, verified by `--check`, both
-- modelled on pipelines/ws13_migrate.py: every statement is idempotent,
-- rerunning the file is a no-op, and a rerun over a table created by an
-- earlier version of this file adds what that version lacked.
--
-- Nothing here touches ws13_documents, ws13_pages, ws13_chunks,
-- ws13_manifest or ws13_mine_id_map; the driver only READS those.


-- 1. the ledger ------------------------------------------------------------
--
-- mine_key is the FRONT-END key the model is filed under (grades:<row>,
-- stategeo:<id>, mrds:<dep_id>, usmin:<fid>, ardf:<n>) or '-' for a document
-- decided before any mine was named (no mine ids or names, refused rights,
-- parked by the resolver). It is part of the primary key, so it cannot be
-- NULL; '-' is the documented sentinel and the driver never builds under it.
CREATE TABLE IF NOT EXISTS ws13_geomodel_runs (
    sha256       TEXT NOT NULL,
    mine_key     TEXT NOT NULL,
    run_id       TEXT NOT NULL,
    status       TEXT NOT NULL,
    reason       TEXT,
    model_id     TEXT,
    content_hash TEXT,
    counts       JSONB,
    warnings     JSONB,
    attempts     INT NOT NULL DEFAULT 0,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (sha256, mine_key)
);

-- Columns added one at a time, IF NOT EXISTS, so a table created by an
-- earlier shape of this file gains what it lacks instead of failing.
ALTER TABLE ws13_geomodel_runs ADD COLUMN IF NOT EXISTS reason TEXT;
ALTER TABLE ws13_geomodel_runs ADD COLUMN IF NOT EXISTS model_id TEXT;
ALTER TABLE ws13_geomodel_runs ADD COLUMN IF NOT EXISTS content_hash TEXT;
ALTER TABLE ws13_geomodel_runs ADD COLUMN IF NOT EXISTS counts JSONB;
ALTER TABLE ws13_geomodel_runs ADD COLUMN IF NOT EXISTS warnings JSONB;
ALTER TABLE ws13_geomodel_runs
    ADD COLUMN IF NOT EXISTS attempts INT NOT NULL DEFAULT 0;
ALTER TABLE ws13_geomodel_runs
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now();


-- 2. the status vocabulary --------------------------------------------------
--
-- Six statuses and no seventh. The driver's exit code, heartbeat and
-- --verify-complete all group by this column, so a misspelt status written by
-- a future edit must fail at the INSERT rather than become a silent seventh
-- bucket that "remaining" never counts down. Rebuilt drop-and-add rather than
-- widened in place, the way ws13_mine_id_map.py rebuilds its method CHECK,
-- because PostgreSQL has no ALTER CONSTRAINT for a CHECK expression; NOT
-- VALID then VALIDATE keeps the ACCESS EXCLUSIVE lock to the catalog write.
ALTER TABLE ws13_geomodel_runs
    DROP CONSTRAINT IF EXISTS ws13_geomodel_runs_status;
ALTER TABLE ws13_geomodel_runs
    ADD CONSTRAINT ws13_geomodel_runs_status CHECK (
        status IN ('planned', 'parked', 'skipped', 'built', 'published',
                   'error'))
    NOT VALID;
ALTER TABLE ws13_geomodel_runs VALIDATE CONSTRAINT ws13_geomodel_runs_status;

-- A published row names its model and its content hash, or it is not a
-- published row: the rerun-safety test compares both, and a row that passed
-- one without the other would be skipped forever on a model nobody can open.
ALTER TABLE ws13_geomodel_runs
    DROP CONSTRAINT IF EXISTS ws13_geomodel_runs_published;
ALTER TABLE ws13_geomodel_runs
    ADD CONSTRAINT ws13_geomodel_runs_published CHECK (
        status <> 'published'
        OR (model_id IS NOT NULL AND content_hash IS NOT NULL))
    NOT VALID;
ALTER TABLE ws13_geomodel_runs
    VALIDATE CONSTRAINT ws13_geomodel_runs_published;


-- 3. the reads the driver and the operator make ------------------------------
--
-- status: "parked by reason" and "remaining" are GROUP BY / anti-join on it.
-- run_id: everything one sweep wrote, for an operator reading a heartbeat.
-- model_id: which (document, mine) rows a published model came from, which
-- is how a model directory is traced back to its ledger rows before a prune.
CREATE INDEX IF NOT EXISTS ws13_geomodel_runs_status
    ON ws13_geomodel_runs (status);
CREATE INDEX IF NOT EXISTS ws13_geomodel_runs_run_id
    ON ws13_geomodel_runs (run_id);
CREATE INDEX IF NOT EXISTS ws13_geomodel_runs_model_id
    ON ws13_geomodel_runs (model_id)
    WHERE model_id IS NOT NULL;


-- 4. the guard --------------------------------------------------------------
--
-- ADD CONSTRAINT above cannot be told "IF NOT EXISTS", and DROP + ADD is what
-- makes it idempotent; this block is what makes it CORRECT. It fails the
-- migration (and the transaction ws13_geomodel.py --migrate wraps it in) if
-- the status constraint that ended up in the catalog does not name every one
-- of the six statuses, so a hand-edited constraint cannot survive a rerun.
DO $status_guard$
DECLARE
    definition TEXT;
    status_name TEXT;
BEGIN
    SELECT pg_get_constraintdef(c.oid) INTO definition
      FROM pg_constraint c
     WHERE c.conrelid = 'ws13_geomodel_runs'::regclass
       AND c.conname = 'ws13_geomodel_runs_status';
    IF definition IS NULL THEN
        RAISE EXCEPTION 'ws13_geomodel_runs_status is missing after ADD CONSTRAINT';
    END IF;
    FOREACH status_name IN ARRAY ARRAY['planned', 'parked', 'skipped',
                                       'built', 'published', 'error']
    LOOP
        IF position(quote_literal(status_name) IN definition) = 0 THEN
            RAISE EXCEPTION 'ws13_geomodel_runs_status does not admit %: %',
                status_name, definition;
        END IF;
    END LOOP;
END
$status_guard$;


-- 5. the reader -------------------------------------------------------------
--
-- ws13_reader (pipelines/ws13_migrations.sql section 5) is SELECT-only and
-- may not exist on a database this file reaches before that one. Guarded, so
-- a missing role cannot abort the ledger migration; the ALTER DEFAULT
-- PRIVILEGES in that file already covers a table created after it ran.
DO $reader_grant$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ws13_reader') THEN
        GRANT SELECT ON ws13_geomodel_runs TO ws13_reader;
    END IF;
END
$reader_grant$;
