"""Prove upsert semantics using the SAME generated SQL, against in-memory SQLite.

Verifies: new rows insert, existing rows update, and Status + fit_score + first_seen
are preserved across a re-scrape.
Run:  python -m tests.test_storage_upsert
"""
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jobtracker.adapters import parse_recruitee_offer
from jobtracker.storage import build_upsert_sql, job_row, INSERT_COLUMNS

FIXTURE = Path(__file__).parent / "fixtures" / "genomescan_offers.json"

SQLITE_DDL = """
CREATE TABLE jobs (
    job_id      TEXT PRIMARY KEY,
    title       TEXT NOT NULL,
    company     TEXT NOT NULL,
    source      TEXT NOT NULL,
    url         TEXT,
    location    TEXT,
    remote      INTEGER NOT NULL DEFAULT 0,
    salary      TEXT,
    description TEXT,
    posted_date TEXT,
    status      TEXT NOT NULL DEFAULT 'New',
    fit_score   INTEGER,
    fit_reason  TEXT,
    fit_persona TEXT,
    first_seen  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_seen   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


def load_jobs():
    offers = json.loads(FIXTURE.read_text(encoding="utf-8"))["offers"]
    return [parse_recruitee_offer(o, "GenomeScan") for o in offers]


def test_upsert_preserves_status_and_score():
    con = sqlite3.connect(":memory:")
    con.executescript(SQLITE_DDL)
    sql = build_upsert_sql(placeholder="?", now="CURRENT_TIMESTAMP")

    jobs = load_jobs()
    for j in jobs:
        con.execute(sql, job_row(j))
    con.commit()
    assert con.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 3

    key = jobs[0].job_id
    first_seen = con.execute("SELECT first_seen FROM jobs WHERE job_id=?", (key,)).fetchone()[0]

    # user + LLM enrich the row
    con.execute("UPDATE jobs SET status='Applied', fit_score=88 WHERE job_id=?", (key,))
    con.commit()

    # next scrape returns same job with updated title (but no score) -> re-upsert
    jobs[0].title = "Director Commercial Laboratory (updated)"
    for j in jobs:
        con.execute(sql, job_row(j))
    con.commit()

    row = con.execute(
        "SELECT title, status, fit_score, first_seen FROM jobs WHERE job_id=?", (key,)
    ).fetchone()
    assert con.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 3
    assert row[0].endswith("(updated)"), "title refreshed"
    assert row[1] == "Applied", "status preserved"
    assert row[2] == 88, "fit_score preserved"
    assert row[3] == first_seen, "first_seen preserved"
    print("PASS test_upsert_preserves_status_and_score")


def test_sql_shape():
    sql = build_upsert_sql("%s", "now()", returning="RETURNING (xmax = 0) AS inserted")
    assert sql.count("%s") == len(INSERT_COLUMNS) == 14
    assert "ON CONFLICT (job_id) DO UPDATE" in sql
    set_clause = sql.split("DO UPDATE SET", 1)[1]
    # status is fully user-owned: never written on conflict
    assert "status" not in set_clause
    # score/reason use COALESCE: new value wins, else keep existing
    assert "fit_score = COALESCE(EXCLUDED.fit_score, jobs.fit_score)" in sql
    assert "fit_reason = COALESCE(EXCLUDED.fit_reason, jobs.fit_reason)" in sql
    assert "last_seen = now()" in sql
    print("PASS test_sql_shape")


def test_fresh_score_writes_onto_existing_null():
    """Regression: a row inserted before scoring (fit_score NULL) must accept a
    freshly computed score on the next upsert."""
    con = sqlite3.connect(":memory:")
    con.executescript(SQLITE_DDL)
    sql = build_upsert_sql(placeholder="?", now="CURRENT_TIMESTAMP")
    jobs = load_jobs()
    for j in jobs:
        con.execute(sql, job_row(j))          # inserted with NULL score
    con.commit()
    key = jobs[0].job_id
    assert con.execute("SELECT fit_score FROM jobs WHERE job_id=?", (key,)).fetchone()[0] is None

    jobs[0].fit_score, jobs[0].fit_reason = 91, "strong match"
    con.execute(sql, job_row(jobs[0]))         # re-upsert now that it's scored
    con.commit()
    row = con.execute("SELECT fit_score, fit_reason FROM jobs WHERE job_id=?", (key,)).fetchone()
    assert row == (91, "strong match"), row
    print("PASS test_fresh_score_writes_onto_existing_null")


if __name__ == "__main__":
    test_sql_shape()
    test_fresh_score_writes_onto_existing_null()
    test_upsert_preserves_status_and_score()
    print("\nAll storage upsert tests passed.")
