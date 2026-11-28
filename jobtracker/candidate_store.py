"""Candidate profile storage (see /workspace/plans/job-tracker-candidate-profile-upload.md):
a single row, not a list of jobs, so it gets its own tiny store mirroring
jobtracker/generation_records.py's Postgres/local-JSON split.

`candidate_data` is the structured fact bank -- same shape as
config/candidate.yaml (personal/experience/education/skills/languages) --
and `style_example` is plain text extracted from an uploaded cover letter
.docx, used as a style guide the same way generator.example_letter_text()
uses Solliciteren/Motivatie.docx today.

The resume and the cover letter are reviewed and saved as two SEPARATE
actions on the /settings/candidate page (issue: parse -> review -> save
must not clobber the other half of the profile), so
save_candidate_data()/save_style_example() each update one field without
touching the other -- never a single "set everything" call.
"""
from __future__ import annotations

import datetime
import json
import os
import threading
import uuid
from pathlib import Path
from typing import Optional


class PostgresCandidateProfileStore:
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

    def get(self) -> Optional[dict]:
        """None only when the row itself has never been created (first-run /
        empty database) -- NOT a guarantee that both fields are populated.
        save_candidate_data() and save_style_example() are separate actions
        (resume and cover letter are reviewed/saved independently), so a row
        that exists after only one of them has ever run has the OTHER field
        still null -- callers must handle candidate_data/style_example being
        None independently of whether get() itself returned None.

        `updated_at` is normalized to an ISO 8601 string here so callers get
        the same type from either backend -- psycopg2 decodes TIMESTAMPTZ
        into a real `datetime`, while the local-JSON store's equivalent
        value is already a plain isoformat() string (see
        generation_records.py's `_month_key()` for the same datetime/str
        split on this exact column type, there too fixed by normalizing
        rather than branching at every call site)."""
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT candidate_data, style_example, updated_at "
                "FROM candidate_profile WHERE profile_id = 'default'")
            row = cur.fetchone()
            if row is None:
                return None
            candidate_data, style_example, updated_at = row
            if updated_at is not None:
                updated_at = updated_at.isoformat()
            return {"candidate_data": candidate_data, "style_example": style_example,
                   "updated_at": updated_at}

    def save_candidate_data(self, candidate_data: dict) -> None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO candidate_profile (profile_id, candidate_data) "
                "VALUES ('default', %s) "
                "ON CONFLICT (profile_id) DO UPDATE SET "
                "candidate_data = EXCLUDED.candidate_data, updated_at = now()",
                (json.dumps(candidate_data),))
            conn.commit()

    def save_style_example(self, style_example: str) -> None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO candidate_profile (profile_id, style_example) "
                "VALUES ('default', %s) "
                "ON CONFLICT (profile_id) DO UPDATE SET "
                "style_example = EXCLUDED.style_example, updated_at = now()",
                (style_example,))
            conn.commit()


# webapp.py constructs a FRESH store instance per request (same reasoning as
# generation_records.py's _WRITE_LOCK) -- only a lock shared across every
# instance actually serializes the read-modify-write below.
_WRITE_LOCK = threading.Lock()


class LocalJSONCandidateProfileStore:
    def __init__(self, path: str = "data/candidate_profile.json"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _load(self) -> dict:
        if self.path.exists():
            return json.loads(self.path.read_text(encoding="utf-8"))
        return {}

    def _write(self, data: dict) -> None:
        # Atomic rename (not an in-place write), same as generation_records.py,
        # so a concurrent read of the file never sees a truncated partial write.
        tmp_path = self.path.with_suffix(self.path.suffix + f".tmp-{uuid.uuid4().hex}")
        tmp_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp_path, self.path)

    def get(self) -> Optional[dict]:
        data = self._load()
        if not data:
            return None
        return {"candidate_data": data.get("candidate_data"),
               "style_example": data.get("style_example"),
               "updated_at": data.get("updated_at")}

    def save_candidate_data(self, candidate_data: dict) -> None:
        with _WRITE_LOCK:
            data = self._load()
            data["candidate_data"] = candidate_data
            data["updated_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
            self._write(data)

    def save_style_example(self, style_example: str) -> None:
        with _WRITE_LOCK:
            data = self._load()
            data["style_example"] = style_example
            data["updated_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
            self._write(data)


def make_candidate_store():
    """Pick backend by configuration, same signal as storage.make_store() /
    generation_records.make_generation_record_store()."""
    if os.getenv("DATABASE_URL") or os.getenv("PGHOST") or os.getenv("PGDATABASE"):
        return PostgresCandidateProfileStore()
    return LocalJSONCandidateProfileStore()
