-- WS13 retrieval/citation migrations: the columns, indexes, function and
-- read-only role the citation contract needs and that production does not
-- have. Applied by pipelines/ws13_migrate.py; also executed by
-- pipelines/ws13_seed.py so a rebuild from scratch reproduces production.
--
-- What is broken today, concretely:
--
--   * ws13_documents has no admission_class. The rights class only exists as
--     the second path segment of s3_key ('ws12/originals/...',
--     'ws12/licensed-copies/...', 'ws12/research-copies/...'), so every
--     rights decision would have to re-parse a string at query time.
--   * ws13_documents has no source_url and no rights_basis AT ALL. The
--     citation payload requires a resolvable title/page/source and rights
--     terms that travel with the document; without these two columns the
--     payload cannot be built -- not "built badly", not built. That is why
--     they are in this migration and not a later one: 13,013 licensed AZGS
--     ADMMR copies and 32,312 IGS research copies are only defensible to
--     serve because attribution travels with them.
--   * doc_date is free text. Measured across the 106,396 harvest-manifest
--     rows: 76,681 NULL, 27,882 bare 'YYYY', 53 'YYYY-MM', 1,780 other --
--     and "other" is literally 'VARIOUS', 'CIRCA 1980', '1930; 1933; 1940'.
--     A year filter written against doc_date drops ~72% of rows to the NULL
--     comparison alone and mis-sorts every non-'YYYY' value on top of that.
--   * county is stored two ways: 15,581 rows end in ' County' and 51,685 are
--     bare. An equality predicate matches whichever half of the corpus the
--     caller happened to type and silently misses the other.
--
-- Every statement below is idempotent; rerunning the file is a no-op and
-- reports zero changed rows. pipelines/ws13_migrate.py runs the whole file in
-- one transaction, so a partial application is impossible.
--
-- NOT IN THIS FILE: the HNSW index on titan_embedding
-- (ws13_chunks_titan_hnsw over titan_embedding::halfvec(1024)). That is a
-- long, memory-tuned build over 852,027 rows with its own ANALYZE and EXPLAIN
-- proof, and it is owned by pipelines/ws13_build_ann_index.py. Putting it here
-- would turn an ordinary schema migration into an hours-long job holding locks
-- nobody expects.


-- 1. admission_class -----------------------------------------------------
--
-- GENERATED ALWAYS ... STORED rather than a plain backfilled column, because
-- the storage prefix IS the right: a generated column cannot drift away from
-- where the bytes actually live, and no backfill pass can forget a document.
ALTER TABLE ws13_documents
    ADD COLUMN IF NOT EXISTS admission_class TEXT
    GENERATED ALWAYS AS (split_part(s3_key, '/', 2)) STORED;

-- The definition is checked by the shared generated-column guard at the end
-- of section 2, once the year columns exist too: ADD COLUMN IF NOT EXISTS is
-- silent about a column that already exists with the WRONG definition, and a
-- wrong admission_class is a rights error, not a cosmetic one.

CREATE INDEX IF NOT EXISTS ws13_documents_admission
    ON ws13_documents (admission_class);


-- 2. doc_year_min / doc_year_max -----------------------------------------
--
-- Extract every 4-digit run from the free-text doc_date and keep the range.
-- '1930; 1933; 1940' becomes 1930..1940, 'CIRCA 1980' becomes 1980..1980,
-- and 'VARIOUS' stays NULL rather than inventing a year.
--
-- The 1800..2099 bound is not decoration: '\d{4}' also matches the '0601' in
-- a packed date like '19740601' and any 4-digit report number that happens to
-- be in the string.
--
-- IMMUTABLE helper + GENERATED ALWAYS ... STORED, NOT a backfill UPDATE. The
-- earlier form of this section filled the two columns once and left no
-- maintenance path: ws13_worker.py's INSERT INTO ws13_documents lists neither
-- year column and its ON CONFLICT DO UPDATE does not touch them, so every
-- document indexed after the migration would have a parseable doc_date and a
-- NULL year range -- and infra/ws13_query_lambda.py's 'd.doc_year_max >= %s'
-- would then drop exactly the newest rows, silently, which is the failure
-- the columns were added to prevent. 27,882 of 106,396 harvest-manifest rows
-- carry a bare 'YYYY', so that is the population at stake. Same argument as
-- admission_class above: no backfill pass can forget a document if there is
-- no backfill pass.
CREATE OR REPLACE FUNCTION ws13_doc_year_min(text) RETURNS int
    LANGUAGE sql
    IMMUTABLE
    PARALLEL SAFE
AS $fn$
    SELECT min(y.year_value)
      FROM (SELECT (m.hit)[1]::INT AS year_value
              FROM regexp_matches(coalesce($1, ''), '\d{4}', 'g') AS m(hit)
           ) y
     WHERE y.year_value BETWEEN 1800 AND 2099
$fn$;

CREATE OR REPLACE FUNCTION ws13_doc_year_max(text) RETURNS int
    LANGUAGE sql
    IMMUTABLE
    PARALLEL SAFE
AS $fn$
    SELECT max(y.year_value)
      FROM (SELECT (m.hit)[1]::INT AS year_value
              FROM regexp_matches(coalesce($1, ''), '\d{4}', 'g') AS m(hit)
           ) y
     WHERE y.year_value BETWEEN 1800 AND 2099
$fn$;

-- Two ways an existing database arrives here with year columns that are not
-- maintained, both repaired by dropping and re-adding rather than by asking
-- an operator to notice:
--
--   * an earlier run of this file created them as plain INT columns filled by
--     the one-shot UPDATE. Every document indexed since is NULL;
--   * they are generated, but the helper above was edited afterwards.
--     CREATE OR REPLACE FUNCTION does not recompute stored values, so the
--     column silently keeps the old parse.
--
-- Dropping is safe here and only here: the year range is derived from
-- doc_date and is recomputed in full by the ADD COLUMN below. admission_class
-- gets the opposite treatment -- an unexpected definition there raises, since
-- a surprise in the rights class must stop the deploy for a human. Dropping a
-- column drops ws13_documents_years with it; it is recreated at the end of
-- this section.
DO $year_reset$
DECLARE
    plain_cols INT;
    generated_cols INT;
    stale BIGINT := 0;
BEGIN
    SELECT count(*) FILTER (WHERE a.attgenerated <> 's'),
           count(*) FILTER (WHERE a.attgenerated = 's')
      INTO plain_cols, generated_cols
      FROM pg_attribute a
     WHERE a.attrelid = 'ws13_documents'::regclass
       AND a.attname IN ('doc_year_min', 'doc_year_max')
       AND NOT a.attisdropped;
    IF plain_cols = 0 AND generated_cols < 2 THEN
        RETURN;             -- nothing yet, or half-added: the ALTER adds it
    END IF;
    IF plain_cols = 0 THEN
        -- Both are generated. The only remaining way they can be wrong is a
        -- helper that was edited after the values were stored; this is the
        -- one statement in the file that reads all 56,282 rows, and it reads
        -- metadata columns only.
        SELECT count(*) INTO stale FROM ws13_documents
         WHERE doc_year_min IS DISTINCT FROM ws13_doc_year_min(doc_date)
            OR doc_year_max IS DISTINCT FROM ws13_doc_year_max(doc_date);
        IF stale = 0 THEN
            RETURN;
        END IF;
        RAISE NOTICE 'rebuilding doc_year_min/max: % rows disagree', stale;
    ELSE
        RAISE NOTICE 'rebuilding doc_year_min/max: % not generated', plain_cols;
    END IF;
    ALTER TABLE ws13_documents DROP COLUMN IF EXISTS doc_year_min;
    ALTER TABLE ws13_documents DROP COLUMN IF EXISTS doc_year_max;
END
$year_reset$;

ALTER TABLE ws13_documents
    ADD COLUMN IF NOT EXISTS doc_year_min INT
        GENERATED ALWAYS AS (ws13_doc_year_min(doc_date)) STORED,
    ADD COLUMN IF NOT EXISTS doc_year_max INT
        GENERATED ALWAYS AS (ws13_doc_year_max(doc_date)) STORED;

-- Fail closed on a half-applied earlier run. Substring matching was not
-- enough: 'split_part' and 's3_key' both appear in
-- split_part(s3_key, '/', 3), which is the portal segment ('azgs_admmr'),
-- not the rights class, and that spelling passed the old guard. Compare the
-- whole expression, normalised for the '::text' casts and whitespace
-- pg_get_expr emits and for a schema qualification that appears when public
-- is not in search_path. ws13_migrate.py run_checks() applies the same
-- comparison, so --check catches a hand-altered column too.
DO $generated_guard$
DECLARE
    spec RECORD;
    generation_expr TEXT;
    normalized TEXT;
BEGIN
    FOR spec IN
        SELECT * FROM (VALUES
            ('admission_class', 'split_part(s3_key,''/'',2)'),
            ('doc_year_min', 'ws13_doc_year_min(doc_date)'),
            ('doc_year_max', 'ws13_doc_year_max(doc_date)')
        ) AS s(column_name, expected)
    LOOP
        SELECT pg_get_expr(d.adbin, d.adrelid)
          INTO generation_expr
          FROM pg_attrdef d
          JOIN pg_attribute a
            ON a.attrelid = d.adrelid AND a.attnum = d.adnum
         WHERE d.adrelid = 'ws13_documents'::regclass
           AND a.attname = spec.column_name
           AND a.attgenerated = 's';
        -- Single-line RAISE messages on purpose: adjacent-literal
        -- concatenation across lines is legal SQL but easy to break with a
        -- stray edit, and a migration guard must not be the thing that fails
        -- to parse.
        IF generation_expr IS NULL THEN
            RAISE EXCEPTION '% is not a STORED generated column',
                            spec.column_name;
        END IF;
        normalized := replace(replace(
            regexp_replace(generation_expr, '\s+', '', 'g'),
            '::text', ''), 'public.', '');
        IF normalized <> spec.expected
           AND normalized <> '(' || spec.expected || ')' THEN
            RAISE EXCEPTION 'unexpected % generation: % (normalised to %, expected %)',
                            spec.column_name, generation_expr, normalized,
                            spec.expected;
        END IF;
    END LOOP;
END
$generated_guard$;

CREATE INDEX IF NOT EXISTS ws13_documents_years
    ON ws13_documents (doc_year_min, doc_year_max);


-- 3. provenance and rights columns ---------------------------------------
--
-- Not scope creep. The retrieval contract emits, for every hit, a citation
-- carrying document_title, page, source_url and the rights terms implied by
-- the admission class -- including rights_basis, which is the per-collection
-- attribution string the AZGS licence and the IGS research-copy retention
-- rationale are recorded in. ws13_documents stores none of it, and
-- ws13_worker.py never had anywhere to put it: the values are already in the
-- WS12 harvest manifest, keyed on the same sha256. Without these three
-- columns there is no citation payload and no way to make attribution travel.
-- pipelines/ws13_backfill_provenance.py fills them for the 56,282 documents
-- that are already indexed, so no document needs re-OCR to become citable.
--
-- Adding the columns is not the same as filling them, and only the second
-- one makes a citation possible. `ws13_migrate.py --check
-- --require-provenance` is the gate that measures the filling: no licensed
-- or research copy with a NULL rights_basis, and source_url coverage above a
-- threshold. It belongs in front of the WS13 retrieval flag, since a hit on
-- a document whose rights cannot be stated raises out of the query Lambda
-- rather than degrading.
ALTER TABLE ws13_documents
    ADD COLUMN IF NOT EXISTS source_url TEXT,
    ADD COLUMN IF NOT EXISTS rights_basis TEXT,
    ADD COLUMN IF NOT EXISTS public_domain BOOLEAN;


-- 4. ws13_county_key ------------------------------------------------------
--
-- county is stored both as 'Apache County' (15,581 rows) and as 'Apache'
-- (51,685 rows), so any predicate comparing county to a caller-supplied
-- string matches one half of the corpus and silently misses the other. This
-- normalises BOTH sides; callers must apply it to the literal as well as to
-- the column, or the index is useless and the answer is still half-right.
--
-- CALLED ON NULL INPUT (not STRICT) on purpose: coalesce($1,'') maps a NULL
-- county to '' so the function never returns NULL into a comparison.
-- IMMUTABLE because it is a pure text transform, which is what lets it carry
-- an index.
CREATE OR REPLACE FUNCTION ws13_county_key(text) RETURNS text
    LANGUAGE sql
    IMMUTABLE
    PARALLEL SAFE
AS $fn$
    SELECT lower(regexp_replace(coalesce($1, ''), '\s+county$', '', 'i'))
$fn$;

CREATE INDEX IF NOT EXISTS ws13_documents_county_key
    ON ws13_documents (ws13_county_key(county));


-- 5. ws13_reader ----------------------------------------------------------
--
-- The retrieval Lambda's login role: SELECT and nothing else.
--
-- Created NOLOGIN deliberately, and the password is deliberately not in this
-- file. A LOGIN role with no password authenticates on whatever pg_hba
-- happens to permit, so the role is promoted to LOGIN by
-- `ws13_migrate.py --reader-secret-arn` in the same statement that installs
-- the password, on an in-VPC host reading Secrets Manager directly. The
-- password never reaches a laptop and never reaches this repository.
DO $role$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ws13_reader') THEN
        CREATE ROLE ws13_reader NOLOGIN;
    END IF;
END
$role$;

-- The database name is not knowable statically, so this one grant is dynamic.
DO $grant_connect$
BEGIN
    EXECUTE format('GRANT CONNECT ON DATABASE %I TO ws13_reader',
                   current_database());
END
$grant_connect$;

GRANT USAGE ON SCHEMA public TO ws13_reader;

-- The four tables the retrieval path actually reads. Ungarded on purpose: if
-- one of these is missing the migration must abort rather than hand out a
-- partial grant that looks like success.
GRANT SELECT ON ws13_documents, ws13_pages, ws13_chunks, ws13_manifest
    TO ws13_reader;

-- ws13_mine_id_map is produced by a later WS13 stage and may not exist yet;
-- ws13_embed_skips already does. Guarded so a table that has not been built
-- cannot abort the whole migration, and written as a loop so a future ws13_*
-- table created before this file is rerun is picked up without an edit. The
-- backslash escapes the LIKE wildcard so 'ws13_' matches literally.
DO $grant_rest$
DECLARE
    table_name TEXT;
BEGIN
    FOR table_name IN
        SELECT tablename
          FROM pg_tables
         WHERE schemaname = 'public'
           AND tablename LIKE 'ws13\_%'
    LOOP
        EXECUTE format('GRANT SELECT ON public.%I TO ws13_reader',
                       table_name);
    END LOOP;
END
$grant_rest$;

-- Later WS13 tables stay readable without another migration round.
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT ON TABLES TO ws13_reader;

-- Fail closed on write: if an earlier hand-run over-granted, take it back.
-- The retrieval Lambda must not be able to modify the corpus it cites, and
-- must not be able to create objects in the schema it reads.
REVOKE INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER
    ON ALL TABLES IN SCHEMA public FROM ws13_reader;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    REVOKE INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER
    ON TABLES FROM ws13_reader;
REVOKE CREATE ON SCHEMA public FROM ws13_reader;
