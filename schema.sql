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
