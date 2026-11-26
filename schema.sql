-- job-tracker Postgres schema. Applied automatically by the pipeline on first run,
-- but you can also run it by hand:  psql "$DATABASE_URL" -f schema.sql

CREATE TABLE IF NOT EXISTS jobs (
    job_id       TEXT PRIMARY KEY,
    title        TEXT NOT NULL,
    company      TEXT NOT NULL,
    source       TEXT NOT NULL,
    url          TEXT,
    location     TEXT,
    remote       BOOLEAN     NOT NULL DEFAULT FALSE,
    salary       TEXT,
    description  TEXT,
    posted_date  DATE,
    status       TEXT        NOT NULL DEFAULT 'New',
    fit_score    INTEGER,
    fit_reason   TEXT,
    fit_persona  TEXT,
    first_seen   TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Migrations for existing databases:
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS fit_score   INTEGER;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS fit_reason  TEXT;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS fit_persona TEXT;

CREATE INDEX IF NOT EXISTS jobs_company_idx ON jobs (company);
CREATE INDEX IF NOT EXISTS jobs_status_idx  ON jobs (status);
CREATE INDEX IF NOT EXISTS jobs_seen_idx    ON jobs (first_seen);
CREATE INDEX IF NOT EXISTS jobs_fit_idx     ON jobs (fit_score);

-- Snapshot of a vacancy ingested from a bare URL/pasted text (for CV/letter
-- generation that didn't start from an already-scraped job). Kept even after
-- the posting goes offline, so the text is still available before an
-- interview. Separate from `jobs` since these aren't scraped/scored sources.
CREATE TABLE IF NOT EXISTS vacancy_snapshots (
    snapshot_id     TEXT PRIMARY KEY,
    url             TEXT NOT NULL,
    job_id          TEXT,
    platform        TEXT,
    raw_source      TEXT,
    extracted_text  TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS vacancy_snapshots_job_idx ON vacancy_snapshots (job_id);

-- One row per analyse/tailor Claude API call (issue #10), so model id,
-- token usage, stop_reason, and the raw response are auditable per job --
-- not just printed to the console.
CREATE TABLE IF NOT EXISTS generation_records (
    record_id       TEXT PRIMARY KEY,
    job_id          TEXT NOT NULL,
    stage           TEXT NOT NULL,  -- 'analyse' | 'tailor'
    model           TEXT NOT NULL,
    stop_reason     TEXT,
    input_tokens    INTEGER,
    output_tokens   INTEGER,
    raw_response    JSONB,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- issue #13: prompt-cache token usage, so a cached tailor call's true cost
-- (cache write/read tokens are priced differently from plain input tokens)
-- is computable from this table, not just its non-cached input_tokens.
ALTER TABLE generation_records ADD COLUMN IF NOT EXISTS cache_creation_input_tokens INTEGER;
ALTER TABLE generation_records ADD COLUMN IF NOT EXISTS cache_read_input_tokens     INTEGER;

CREATE INDEX IF NOT EXISTS generation_records_job_idx ON generation_records (job_id);
