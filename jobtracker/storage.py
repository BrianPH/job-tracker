"""Storage backends: Postgres (primary), local JSON (fallback/testing), Airtable (optional).

All backends upsert on Job.job_id so re-runs don't create duplicates. On update,
`status`, `fit_score`, `fit_reason`, and `first_seen` are preserved — so your edits
and the LLM scores survive the next scrape.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import List, Optional, Set

import requests

from .models import Job

TIMEOUT = 30

INSERT_COLUMNS = [
    "job_id", "title", "company", "source", "url", "location",
    "remote", "salary", "description", "posted_date", "status",
    "fit_score", "fit_reason", "fit_persona",
]
# On conflict, refresh these from the incoming row. Deliberately excludes
# job_id (key), status + fit_score + fit_reason (user/LLM-owned), first_seen (set once).
CONFLICT_UPDATE_COLUMNS = [
    "title", "company", "source", "url", "location",
    "remote", "salary", "description", "posted_date",
]
# Written only when the incoming value is non-null; otherwise the EXISTING value is
# kept. This lets a freshly computed score land on a row that had none, while a plain
# re-scrape (no score) never wipes an existing score.
COALESCE_COLUMNS = ["fit_score", "fit_reason", "fit_persona"]
# Preserved on re-scrape when the incoming value is empty (local JSON backend).
_PRESERVE_IF_EMPTY = ("fit_score", "fit_reason", "fit_persona")


def build_upsert_sql(placeholder: str = "%s", now: str = "now()", returning: str = "") -> str:
    """Build INSERT ... ON CONFLICT upsert. Shared so tests exercise the real SQL."""
    cols = ", ".join(INSERT_COLUMNS)
    phs = ", ".join([placeholder] * len(INSERT_COLUMNS))
    set_parts = [f"{c} = EXCLUDED.{c}" for c in CONFLICT_UPDATE_COLUMNS]
    set_parts += [f"{c} = COALESCE(EXCLUDED.{c}, jobs.{c})" for c in COALESCE_COLUMNS]
    sets = ", ".join(set_parts)
    sql = (
        f"INSERT INTO jobs ({cols}) VALUES ({phs}) "
        f"ON CONFLICT (job_id) DO UPDATE SET {sets}, last_seen = {now}"
    )
    if returning:
        sql += f" {returning}"
    return sql


def job_row(job: Job) -> tuple:
    """Values tuple in INSERT_COLUMNS order, with empty posted_date -> NULL."""
    rec = job.to_record()
    vals = []
    for col in INSERT_COLUMNS:
        v = rec.get(col)
        if col in ("posted_date", "fit_reason", "fit_persona") and not v:
            v = None
        vals.append(v)
    return tuple(vals)


class PostgresStore:
    def __init__(self, dsn: Optional[str] = None):
        import psycopg2
        self._pg = psycopg2
        self.dsn = dsn if dsn is not None else (os.getenv("DATABASE_URL") or "")
        self.ensure_schema()

    def _connect(self):
        return self._pg.connect(self.dsn) if self.dsn else self._pg.connect()

    def ensure_schema(self) -> None:
        ddl = (Path(__file__).resolve().parents[1] / "schema.sql").read_text(encoding="utf-8")
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(ddl)
            conn.commit()

    def upsert(self, jobs: List[Job]) -> dict:
        sql = build_upsert_sql("%s", "now()", returning="RETURNING (xmax = 0) AS inserted")
        inserted = updated = 0
        with self._connect() as conn, conn.cursor() as cur:
            for job in jobs:
                cur.execute(sql, job_row(job))
                if cur.fetchone()[0]:
                    inserted += 1
                else:
                    updated += 1
            conn.commit()
        return {"added": inserted, "updated": updated, "total": len(jobs)}

    def scored_ids(self) -> Set[str]:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT job_id FROM jobs WHERE fit_score IS NOT NULL")
            return {r[0] for r in cur.fetchall()}

    def get(self, job_id: str) -> Optional[dict]:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM jobs WHERE job_id = %s", (job_id,))
            row = cur.fetchone()
            if row is None:
                return None
            cols = [d[0] for d in cur.description]
            return dict(zip(cols, row))

    def update_status(self, job_id: str, status: str) -> bool:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("UPDATE jobs SET status = %s WHERE job_id = %s", (status, job_id))
            changed = cur.rowcount > 0
            conn.commit()
            return changed

    def fetch(self, status: Optional[str] = None, company: Optional[str] = None,
              min_score: Optional[int] = None) -> List[dict]:
        q = ("SELECT job_id,title,company,source,location,remote,salary,url,posted_date,"
             "status,fit_score,fit_reason,fit_persona,first_seen FROM jobs")
        conds, params = [], []
        if status:
            conds.append("status = %s")
            params.append(status)
        if company:
            conds.append("company ILIKE %s")
            params.append(f"%{company}%")
        if min_score is not None:
            conds.append("fit_score >= %s")
            params.append(min_score)
        if conds:
            q += " WHERE " + " AND ".join(conds)
        q += " ORDER BY fit_score DESC NULLS LAST, first_seen DESC"
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(q, params)
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]


class LocalJSONStore:
    def __init__(self, path: str = "data/jobs.json"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _load(self) -> dict:
        if self.path.exists():
            return json.loads(self.path.read_text(encoding="utf-8"))
        return {}

    def upsert(self, jobs: List[Job]) -> dict:
        existing = self._load()
        added = updated = 0
        for job in jobs:
            rec = job.to_record()
            if job.job_id in existing:
                old = existing[job.job_id]
                rec["status"] = old.get("status", rec["status"])
                for k in _PRESERVE_IF_EMPTY:
                    if rec.get(k) in (None, "") and old.get(k) not in (None, ""):
                        rec[k] = old[k]
                if old != rec:
                    updated += 1
            else:
                added += 1
            existing[job.job_id] = rec
        self.path.write_text(json.dumps(existing, indent=2, ensure_ascii=False), encoding="utf-8")
        return {"added": added, "updated": updated, "total": len(existing)}

    def scored_ids(self) -> Set[str]:
        return {k for k, v in self._load().items() if v.get("fit_score") is not None}

    def get(self, job_id: str) -> Optional[dict]:
        return self._load().get(job_id)

    def update_status(self, job_id: str, status: str) -> bool:
        data = self._load()
        if job_id not in data:
            return False
        data[job_id]["status"] = status
        self.path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        return True

    def fetch(self, status: Optional[str] = None, company: Optional[str] = None,
              min_score: Optional[int] = None) -> List[dict]:
        rows = list(self._load().values())
        if status:
            rows = [r for r in rows if r.get("status") == status]
        if company:
            rows = [r for r in rows if company.lower() in r.get("company", "").lower()]
        if min_score is not None:
            rows = [r for r in rows if (r.get("fit_score") or -1) >= min_score]
        rows.sort(key=lambda r: (r.get("fit_score") is not None, r.get("fit_score") or 0), reverse=True)
        return rows


class AirtableStore:
    FIELD_MAP = {
        "title": "Title", "company": "Company", "location": "Location",
        "remote": "Remote", "salary": "Salary", "url": "URL",
        "description": "Description", "posted_date": "Posted date",
        "source": "Source", "job_id": "Job ID", "status": "Status",
        "fit_score": "Fit score", "fit_reason": "Fit reason",
        "fit_persona": "Fit persona",
    }

    def __init__(self, token: str, base_id: str, table_name: str = "Jobs"):
        self.base_url = f"https://api.airtable.com/v0/{base_id}/{requests.utils.quote(table_name)}"
        self.headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    def _fields(self, job: Job) -> dict:
        rec = job.to_record()
        out = {}
        for k, col in self.FIELD_MAP.items():
            v = rec.get(k)
            if k in ("posted_date", "fit_score") and v in (None, ""):
                continue
            out[col] = v
        return out

    def upsert(self, jobs: List[Job]) -> dict:
        created = updated = 0
        for i in range(0, len(jobs), 10):
            batch = jobs[i:i + 10]
            payload = {
                "performUpsert": {"fieldsToMergeOn": ["Job ID"]},
                "records": [{"fields": self._fields(j)} for j in batch],
                "typecast": True,
            }
            resp = requests.patch(self.base_url, headers=self.headers, json=payload, timeout=TIMEOUT)
            if resp.status_code >= 400:
                raise RuntimeError(f"Airtable error {resp.status_code}: {resp.text}")
            body = resp.json()
            created += len(body.get("createdRecords", []))
            updated += len(body.get("updatedRecords", []))
        return {"created": created, "updated": updated, "total": len(jobs)}


def make_store():
    """Pick backend by configuration: Postgres > Airtable > local JSON."""
    if os.getenv("DATABASE_URL") or os.getenv("PGHOST") or os.getenv("PGDATABASE"):
        return PostgresStore(), "postgres"
    token, base = os.getenv("AIRTABLE_TOKEN"), os.getenv("AIRTABLE_BASE_ID")
    if token and base:
        return AirtableStore(token, base, os.getenv("AIRTABLE_TABLE_NAME", "Jobs")), "airtable"
    return LocalJSONStore(), "local-json"
