"""One-time import helper (issue #9): draft a candidate.yaml-shaped dict from
raw CV text via an LLM, for manual review before it becomes your real
config/candidate.yaml.

This module never writes to config/candidate.yaml itself -- write_candidate_draft
always takes an explicit output path, so the normal flow is:
    python import_candidate.py my_cv.txt --out config/candidate.draft.yaml
then you read the draft, fix anything wrong or missing, and rename/copy it to
config/candidate.yaml by hand. After that, candidate.yaml is the single
source of facts; this importer is a one-time bootstrap, not something the
app runs on a schedule.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

import yaml

IMPORT_SYSTEM = (
    "You convert a CV/resume's plain text into a structured JSON object matching "
    "this schema EXACTLY:\n"
    "{\n"
    '  "personal": {"name": str, "address": str, "email": str, "phone": str},\n'
    '  "experience": [{"id": slug, "title": str, "title_en": str, "company": str,\n'
    '                  "period": str, "period_en": str,\n'
    '                  "bullets": [{"id": slug, "tags": [str], "nl": str, "en": str}]}],\n'
    '  "education": [{"id": slug, "degree": str, "degree_en": str, "school": str,\n'
    '                 "school_en": str, "period": str, "period_en": str}],\n'
    '  "skills": [{"name": str, "level": str, "level_en": str, "tags": [str]}],\n'
    '  "languages": [{"name": str, "name_en": str, "level": str, "level_en": str}],\n'
    '  "highlights": [str]\n'
    "}\n"
    "Every \"id\" is a short lowercase slug (letters, digits, hyphens), stable and "
    "unique within its own list -- e.g. a role id from company+role, a bullet id "
    "from role+topic.\n"
    "HARD RULES:\n"
    "- Extract ONLY facts stated or clearly implied in the given text. NEVER invent "
    "employers, dates, degrees, skills, or achievements.\n"
    "- If the text doesn't give an English or Dutch version of a bilingual field "
    "(title/title_en, degree/degree_en, a bullet's nl/en, ...), translate the one "
    "you have rather than leaving the other blank -- but mark nothing as invented "
    "that isn't in the source text.\n"
    "- If a field truly has no information in the text, omit it (object fields) or "
    "leave it an empty string/list, never a placeholder like \"N/A\" or \"Unknown\".\n"
    "Output ONLY the JSON object. No markdown, no commentary, no code fence."
)

REQUIRED_TOP_LEVEL = ("personal", "experience", "education", "skills", "languages")


class CandidateImportError(ValueError):
    """Raised when the LLM reply can't be parsed into the candidate.yaml shape."""


def _parse_json_reply(reply: str) -> dict:
    text = (reply or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text[:4].lower() == "json":
            text = text[4:]
        text = text.strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise CandidateImportError(f"Import reply is not valid JSON: {e}") from e
    if not isinstance(data, dict):
        raise CandidateImportError("Import reply must be a JSON object.")
    return data


def _validate_shape(data: dict) -> None:
    missing = [k for k in REQUIRED_TOP_LEVEL if k not in data]
    if missing:
        raise CandidateImportError(f"Import reply is missing required keys: {', '.join(missing)}")
    if not isinstance(data.get("personal"), dict) or not data["personal"].get("name"):
        raise CandidateImportError("Import reply is missing personal.name.")
    for i, role in enumerate(data.get("experience") or []):
        if not role.get("id"):
            raise CandidateImportError(f"experience[{i}] is missing an id.")
        for j, bullet in enumerate(role.get("bullets") or []):
            if not bullet.get("id"):
                raise CandidateImportError(f"experience[{i}].bullets[{j}] is missing an id.")
    for i, edu in enumerate(data.get("education") or []):
        if not edu.get("id"):
            raise CandidateImportError(f"education[{i}] is missing an id.")


def draft_candidate_from_text(cv_text: str, caller: Callable[[str, str], str]) -> dict:
    """Call `caller(system, user) -> str` and parse the reply into a
    candidate.yaml-shaped dict. Raises CandidateImportError if the reply isn't
    valid JSON or is missing a required field -- never returns a partial/guessed
    structure silently."""
    user = f"CV TEXT:\n{cv_text.strip()}\n\nConvert this to the JSON schema now."
    reply = caller(IMPORT_SYSTEM, user)
    data = _parse_json_reply(reply)
    _validate_shape(data)
    return data


def write_candidate_draft(data: dict, path: Path) -> Path:
    """Write the draft for manual review. Never writes to config/candidate.yaml
    itself -- that rename/copy is a deliberate manual step."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=100),
        encoding="utf-8",
    )
    return path
