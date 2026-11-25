"""Generation record storage (issue #10): one row per analyse/tailor API
call, so model id, token usage, stop_reason, and the raw response are
auditable per job afterwards -- not just printed to the console.

Backend choice mirrors storage.make_store() / snapshots.make_snapshot_store():
Postgres when DATABASE_URL (or PGHOST/PGDATABASE) is set, else a local JSON
file. Kept as its own small store, same reasoning as snapshots.py: these
rows aren't scraped/scored jobs.
"""
from __future__ import annotations

import datetime
import json
import os
import uuid
from pathlib import Path
from typing import List, Optional


def record_from_response(job_id: str, stage: str, model: str, response) -> dict:
    """Pulls the fields a generation_records row needs out of an SDK
    ParsedMessage/Message response. Kept separate from the store classes so
    callers (tailor.py, tests) can build the dict without importing a store."""
    usage = getattr(response, "usage", None)
    return {
        "job_id": job_id,
        "stage": stage,
        "model": model,
        "stop_reason": getattr(response, "stop_reason", None),
        "input_tokens": getattr(usage, "input_tokens", None) if usage else None,
        "output_tokens": getattr(usage, "output_tokens", None) if usage else None,
        "raw_response": response.to_dict() if hasattr(response, "to_dict") else {},
    }


class PostgresGenerationRecordStore:
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

    def record(self, job_id: str, stage: str, model: str, stop_reason: Optional[str],
              input_tokens: Optional[int], output_tokens: Optional[int],
              raw_response: dict) -> dict:
        record_id = str(uuid.uuid4())
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO generation_records "
                "(record_id, job_id, stage, model, stop_reason, input_tokens, "
                "output_tokens, raw_response) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (record_id, job_id, stage, model, stop_reason, input_tokens, output_tokens,
                 json.dumps(raw_response)),
            )
            conn.commit()
        return {"record_id": record_id}

    def list_for_job(self, job_id: str) -> List[dict]:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM generation_records WHERE job_id = %s ORDER BY created_at",
                (job_id,))
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]


class LocalJSONGenerationRecordStore:
    def __init__(self, path: str = "data/generation_records.json"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _load(self) -> list:
        if self.path.exists():
            return json.loads(self.path.read_text(encoding="utf-8"))
        return []

    def record(self, job_id: str, stage: str, model: str, stop_reason: Optional[str],
              input_tokens: Optional[int], output_tokens: Optional[int],
              raw_response: dict) -> dict:
        data = self._load()
        record_id = str(uuid.uuid4())
        data.append({
            "record_id": record_id, "job_id": job_id, "stage": stage, "model": model,
            "stop_reason": stop_reason, "input_tokens": input_tokens,
            "output_tokens": output_tokens, "raw_response": raw_response,
            "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        })
        self.path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        return {"record_id": record_id}

    def list_for_job(self, job_id: str) -> List[dict]:
        return [r for r in self._load() if r["job_id"] == job_id]


def make_generation_record_store():
    """Pick backend by configuration, same signal as storage.make_store()."""
    if os.getenv("DATABASE_URL") or os.getenv("PGHOST") or os.getenv("PGDATABASE"):
        return PostgresGenerationRecordStore()
    return LocalJSONGenerationRecordStore()
