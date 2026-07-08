"""Live Postgres integration test. SKIPPED unless DATABASE_URL (or PG* vars) is set.

Run against your local DB after setup:
    DATABASE_URL=postgresql://jobtracker:changeme@localhost:5432/jobtracker \
        python -m tests.test_postgres_integration

Uses job_ids prefixed 'itest:' and deletes them afterward, so it won't touch real rows.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jobtracker.models import Job
from jobtracker.storage import PostgresStore


def main() -> int:
    if not (os.getenv("DATABASE_URL") or os.getenv("PGHOST") or os.getenv("PGDATABASE")):
        print("SKIP: no DATABASE_URL / PG* env set — configure Postgres to run this test.")
        return 0

    store = PostgresStore()
    j = Job(title="Integration Test Role", company="TestCo", source="Test",
            location="Leiden", remote=True, job_id="itest:1")
    try:
        r1 = store.upsert([j])
        assert r1["added"] == 1, r1

        # mark applied, then re-upsert with a changed title
        with store._connect() as conn, conn.cursor() as cur:
            cur.execute("UPDATE jobs SET status='Applied' WHERE job_id='itest:1'")
            conn.commit()
        j.title = "Integration Test Role (v2)"
        r2 = store.upsert([j])
        assert r2["updated"] == 1 and r2["added"] == 0, r2

        rows = store.fetch(company="TestCo")
        row = next(r for r in rows if r["job_id"] == "itest:1")
        assert row["title"] == "Integration Test Role (v2)", row
        assert row["status"] == "Applied", "status preserved"
        print("PASS postgres integration (insert, update, status preserved)")
    finally:
        with store._connect() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM jobs WHERE job_id LIKE 'itest:%'")
            conn.commit()
    return 0


if __name__ == "__main__":
    sys.exit(main())
