"""Vacancy snapshot storage.

Keeps the raw source (HTML page, or the JSON payload for a known-platform
detail call) and the extracted text for a vacancy that was ingested from a
bare URL or pasted text (see ingest.py), keyed by URL. This is what lets a
generation request survive the original posting going offline.

Backend choice mirrors storage.make_store(): Postgres when DATABASE_URL (or
PGHOST/PGDATABASE) is set, else a local JSON file. Kept as its own small
store rather than folded into storage.py's Job stores, since snapshots
aren't scraped/scored jobs.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit, urlunsplit


def snapshot_id(url: str) -> str:
    """Hash a normalized form of the URL: only the scheme and host are
    lowercased (hosts are case-insensitive by spec), the path/query are kept
    as-is since some servers do treat path case as significant."""
    url = url.strip()
    try:
        parts = urlsplit(url)
        normalized = urlunsplit(parts._replace(scheme=parts.scheme.lower(),
                                                netloc=parts.netloc.lower())) if parts.scheme else url
    except ValueError:
        normalized = url
    return hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:16]


class PostgresSnapshotStore:
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

    def save(self, url: str, job_id: str = "", platform: str = "",
             raw_source: str = "", extracted_text: str = "") -> dict:
        # NULLIF(..., '') + COALESCE: an incoming EMPTY raw_source/extracted_text
        # keeps whatever was already saved, rather than blanking it. This matters
        # for the thin-content path, which saves the fetched raw HTML "in case a
        # human wants to look at it later" -- without this, pasting the vacancy
        # text afterwards (platform="paste", raw_source="") would silently wipe
        # that raw HTML from the very row the paste is meant to complete.
        sid = snapshot_id(url)
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO vacancy_snapshots "
                "(snapshot_id, url, job_id, platform, raw_source, extracted_text) "
                "VALUES (%s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (snapshot_id) DO UPDATE SET "
                "job_id = EXCLUDED.job_id, platform = EXCLUDED.platform, "
                "raw_source = COALESCE(NULLIF(EXCLUDED.raw_source, ''), vacancy_snapshots.raw_source), "
                "extracted_text = COALESCE(NULLIF(EXCLUDED.extracted_text, ''), "
                "vacancy_snapshots.extracted_text)",
                (sid, url, job_id, platform, raw_source, extracted_text),
            )
            conn.commit()
        return {"snapshot_id": sid, "url": url}

    def get(self, url: str) -> Optional[dict]:
        sid = snapshot_id(url)
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM vacancy_snapshots WHERE snapshot_id = %s", (sid,))
            row = cur.fetchone()
            if row is None:
                return None
            cols = [d[0] for d in cur.description]
            return dict(zip(cols, row))


class LocalJSONSnapshotStore:
    def __init__(self, path: str = "data/vacancy_snapshots.json"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _load(self) -> dict:
        if self.path.exists():
            return json.loads(self.path.read_text(encoding="utf-8"))
        return {}

    def save(self, url: str, job_id: str = "", platform: str = "",
             raw_source: str = "", extracted_text: str = "") -> dict:
        sid = snapshot_id(url)
        data = self._load()
        existing = data.get(sid) or {}
        # Same empty-overwrite protection as PostgresSnapshotStore.save() -- see
        # the comment there.
        if not raw_source:
            raw_source = existing.get("raw_source", "")
        if not extracted_text:
            extracted_text = existing.get("extracted_text", "")
        data[sid] = {
            "snapshot_id": sid, "url": url, "job_id": job_id, "platform": platform,
            "raw_source": raw_source, "extracted_text": extracted_text,
            "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }
        self.path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        return {"snapshot_id": sid, "url": url}

    def get(self, url: str) -> Optional[dict]:
        return self._load().get(snapshot_id(url))


def make_snapshot_store():
    """Pick backend by configuration, same signal as storage.make_store()."""
    if os.getenv("DATABASE_URL") or os.getenv("PGHOST") or os.getenv("PGDATABASE"):
        return PostgresSnapshotStore()
    return LocalJSONSnapshotStore()
