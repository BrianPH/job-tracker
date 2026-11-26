"""Generation record storage (issue #10): one row per analyse/tailor API
call, so model id, token usage, stop_reason, and the raw response are
auditable per job afterwards -- not just printed to the console.

Backend choice mirrors storage.make_store() / snapshots.make_snapshot_store():
Postgres when DATABASE_URL (or PGHOST/PGDATABASE) is set, else a local JSON
file. Kept as its own small store, same reasoning as snapshots.py: these
rows aren't scraped/scored jobs.

issue #13 adds: cache_creation/cache_read token fields (so a cached call's
true cost is computable, not just its non-cached input_tokens), list_all()
(monthly cost aggregation needs every row, not one job's), and the pricing
table + cost helpers used by jobtracker/webapp.py's /costs page.
"""
from __future__ import annotations

import collections
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
        "cache_creation_input_tokens": getattr(usage, "cache_creation_input_tokens", None) if usage else None,
        "cache_read_input_tokens": getattr(usage, "cache_read_input_tokens", None) if usage else None,
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
              raw_response: dict, cache_creation_input_tokens: Optional[int] = None,
              cache_read_input_tokens: Optional[int] = None) -> dict:
        record_id = str(uuid.uuid4())
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO generation_records "
                "(record_id, job_id, stage, model, stop_reason, input_tokens, "
                "output_tokens, cache_creation_input_tokens, cache_read_input_tokens, "
                "raw_response) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (record_id, job_id, stage, model, stop_reason, input_tokens, output_tokens,
                 cache_creation_input_tokens, cache_read_input_tokens, json.dumps(raw_response)),
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

    def list_all(self) -> List[dict]:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM generation_records ORDER BY created_at")
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
              raw_response: dict, cache_creation_input_tokens: Optional[int] = None,
              cache_read_input_tokens: Optional[int] = None) -> dict:
        data = self._load()
        record_id = str(uuid.uuid4())
        data.append({
            "record_id": record_id, "job_id": job_id, "stage": stage, "model": model,
            "stop_reason": stop_reason, "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_creation_input_tokens": cache_creation_input_tokens,
            "cache_read_input_tokens": cache_read_input_tokens,
            "raw_response": raw_response,
            "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        })
        self.path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        return {"record_id": record_id}

    def list_for_job(self, job_id: str) -> List[dict]:
        return [r for r in self._load() if r["job_id"] == job_id]

    def list_all(self) -> List[dict]:
        return self._load()


def make_generation_record_store():
    """Pick backend by configuration, same signal as storage.make_store()."""
    if os.getenv("DATABASE_URL") or os.getenv("PGHOST") or os.getenv("PGDATABASE"):
        return PostgresGenerationRecordStore()
    return LocalJSONGenerationRecordStore()


# --- cost tracking (issue #13) ----------------------------------------------- #

# $ per 1M tokens. Current as of 2026-11-26 -- Anthropic has no pricing API,
# so this table is hand-maintained; update it when rates change (see
# https://www.anthropic.com/pricing for the current numbers; also check
# jobtracker/scorer.py's ANTHROPIC_MODEL_EXTRACT/ANTHROPIC_MODEL_WRITE for
# which model ids are actually in use, since those drive what shows up in
# real generation_records rows).
MODEL_PRICING = {
    "claude-haiku-4-5-20251001": {"input": 1.00, "output": 5.00},
    "claude-haiku-4-5": {"input": 1.00, "output": 5.00},
    "claude-sonnet-5": {"input": 3.00, "output": 15.00},
}

# Prompt-cache multipliers on the base INPUT price (tailor.py uses the
# default 5-minute ephemeral cache -- see jobtracker/tailor.py's
# `cache_control` usage and the confirmed decision in
# /workspace/plans/job-tracker-cost-tracking.md). A 1-hour cache would use
# 2.0 instead of 1.25 for writes; update both this comment and the constant
# together if tailor.py's TTL ever changes.
CACHE_WRITE_MULTIPLIER = 1.25
CACHE_READ_MULTIPLIER = 0.1


def cost_for_record(record: dict) -> Optional[float]:
    """USD cost of one generation_records row, or None if its model isn't in
    MODEL_PRICING -- returned as None (not 0.0) so a caller can tell "free"
    apart from "unknown", and monthly_cost_summary surfaces the latter as
    `unpriced_calls` rather than silently under-stating spend."""
    pricing = MODEL_PRICING.get(record.get("model"))
    if pricing is None:
        return None
    input_tokens = record.get("input_tokens") or 0
    output_tokens = record.get("output_tokens") or 0
    cache_write = record.get("cache_creation_input_tokens") or 0
    cache_read = record.get("cache_read_input_tokens") or 0
    cost = (
        input_tokens * pricing["input"]
        + cache_write * pricing["input"] * CACHE_WRITE_MULTIPLIER
        + cache_read * pricing["input"] * CACHE_READ_MULTIPLIER
        + output_tokens * pricing["output"]
    ) / 1_000_000
    return cost


def _month_key(record: dict) -> str:
    """"YYYY-MM" for the row's created_at -- a plain string for the local
    JSON store (isoformat(), written by this module) but a `datetime` object
    for the Postgres store (psycopg2 decodes TIMESTAMPTZ columns itself), so
    both shapes need handling here rather than assuming one."""
    created_at = record.get("created_at")
    if isinstance(created_at, datetime.datetime):
        return created_at.strftime("%Y-%m")
    created_at = created_at or ""
    return created_at[:7] if len(created_at) >= 7 else "unknown"


def monthly_cost_summary(records: List[dict]) -> List[dict]:
    """Groups `records` (as returned by list_all()) by the UTC month of
    `created_at` ("YYYY-MM"), newest first. Each entry totals tokens/cost
    across all models that month, plus a `by_model` breakdown -- cheap to
    compute and more useful than a single opaque total for a tool that uses
    two very differently-priced models (Haiku for analyse, Sonnet for
    tailor). `unpriced_calls` counts rows whose model isn't in
    MODEL_PRICING, so a pricing-table gap shows up as a visible gap rather
    than a silently too-low total."""
    months: dict = collections.OrderedDict()
    for record in records:
        key = _month_key(record)
        month = months.setdefault(key, {
            "month": key, "calls": 0, "input_tokens": 0, "output_tokens": 0,
            "cache_creation_tokens": 0, "cache_read_tokens": 0, "cost_usd": 0.0,
            "unpriced_calls": 0, "by_model": {},
        })
        month["calls"] += 1
        month["input_tokens"] += record.get("input_tokens") or 0
        month["output_tokens"] += record.get("output_tokens") or 0
        month["cache_creation_tokens"] += record.get("cache_creation_input_tokens") or 0
        month["cache_read_tokens"] += record.get("cache_read_input_tokens") or 0

        cost = cost_for_record(record)
        model = record.get("model") or "unknown"
        by_model = month["by_model"].setdefault(model, {"calls": 0, "cost_usd": 0.0})
        by_model["calls"] += 1
        if cost is None:
            month["unpriced_calls"] += 1
        else:
            month["cost_usd"] += cost
            by_model["cost_usd"] += cost

    return sorted(months.values(), key=lambda m: m["month"], reverse=True)
