"""Shared Pydantic models for the analyse -> tailor pipeline (issue #10).

Used by both the live Claude API path (jobtracker/tailor.py) and the future
JSON-handoff path (issue #11/M3b: a human runs `/tailor <job_id>` and pastes
the reply back when the API call fails) -- both go through the SAME models
and the SAME validator in this module, so a hand-produced JSON.json is
checked exactly as strictly as a live API response.

Pipeline:
  vacancy text --[analyse, Haiku]--> AnalysisResult
  AnalysisResult + candidate.yaml --[tailor, Sonnet]--> TailorResult
  TailorResult --[validate_tailor_result, pure code]--> list[str] (blocking errors)
                --[ground_tailor_result, pure code]----> list[str] (warnings, non-blocking)
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional

from pydantic import BaseModel, Field

# --------------------------------------------------------------------------- #
# Analyse result                                                              #
# --------------------------------------------------------------------------- #
class ApplicationRequirements(BaseModel):
    """What the vacancy asks for beyond "send a CV and letter" -- surfaced to
    the candidate (notes_for_candidate on TailorResult draws on this), never
    sent to the employer."""
    deadline: str = ""
    page_limit: str = ""
    referees_required: bool = False
    code_sample_required: bool = False
    other: List[str] = Field(default_factory=list)


class AnalysisResult(BaseModel):
    language: str  # "nl" | "en"
    requirements: List[str] = Field(default_factory=list)
    keywords: List[str] = Field(default_factory=list)
    contact: str = ""
    deadline: str = ""
    application_requirements: ApplicationRequirements = Field(
        default_factory=ApplicationRequirements)


# --------------------------------------------------------------------------- #
# Tailor result                                                               #
# --------------------------------------------------------------------------- #
class RequirementMapEntry(BaseModel):
    """Not sent to the employer -- shown on the (future, issue #12) review
    page so the candidate can see what the vacancy asks for that the CV
    doesn't cover."""
    requirement: str
    evidence: List[str] = Field(default_factory=list)  # bullet/fact ids
    status: str = "gap"  # "met" | "partial" | "gap"


class TailorResult(BaseModel):
    language: str  # "nl" | "en"
    profile: str  # 55-85 word CV profile paragraph
    experience_order: List[str] = Field(default_factory=list)  # role ids
    bullets: Dict[str, List[str]] = Field(default_factory=dict)  # role id -> [bullet id]
    skills: List[str] = Field(default_factory=list)  # skill names, ordered subset
    letter_paragraphs: List[str] = Field(default_factory=list)  # 4-6 paragraphs
    requirement_map: List[RequirementMapEntry] = Field(default_factory=list)
    notes_for_candidate: List[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Validator (pure code, no model) -- shared by the live and handoff paths     #
# --------------------------------------------------------------------------- #
PROFILE_WORD_RANGE = (40, 100)  # a little looser than the prompt's 55-85 target
LETTER_WORD_RANGE = (200, 450)  # a little looser than the prompt's 250-400 target


def _word_count(text: str) -> int:
    return len(text.split())


def _candidate_ids(candidate: dict) -> tuple:
    """Returns (role_ids, bullets_by_role, skill_names)."""
    role_ids = set()
    bullets_by_role: Dict[str, set] = {}
    for role in candidate.get("experience", []) or []:
        if not isinstance(role, dict) or not role.get("id"):
            continue
        role_ids.add(role["id"])
        bullets_by_role[role["id"]] = {
            b["id"] for b in (role.get("bullets") or []) if isinstance(b, dict) and b.get("id")
        }
    skill_names = {
        s.get("name", "") for s in (candidate.get("skills") or []) if isinstance(s, dict)
    }
    return role_ids, bullets_by_role, skill_names


def validate_tailor_result(result: TailorResult, candidate: dict,
                           stop_reason: Optional[str] = None) -> List[str]:
    """Blocking errors: a non-empty return means the result must NOT be
    rendered. Checks every id against candidate.yaml, word counts, and the
    stop reason -- never the free-text content itself (that's `ground_*`,
    below, which only warns)."""
    errors: List[str] = []

    if stop_reason == "max_tokens":
        errors.append("Reply was truncated at max_tokens -- treat as a failed generation.")

    role_ids, bullets_by_role, skill_names = _candidate_ids(candidate)

    for role_id in result.experience_order:
        if role_id not in role_ids:
            errors.append(f"experience_order references unknown role id {role_id!r}.")

    for role_id, bullet_ids in result.bullets.items():
        if role_id not in role_ids:
            errors.append(f"bullets references unknown role id {role_id!r}.")
            continue
        for bullet_id in bullet_ids:
            if bullet_id not in bullets_by_role.get(role_id, set()):
                errors.append(
                    f"bullets[{role_id!r}] references unknown bullet id {bullet_id!r}.")

    for skill in result.skills:
        if skill not in skill_names:
            errors.append(f"skills references unknown skill {skill!r}.")

    for i, entry in enumerate(result.requirement_map):
        if entry.status not in ("met", "partial", "gap"):
            errors.append(f"requirement_map[{i}].status must be met/partial/gap, "
                          f"got {entry.status!r}.")

    wc = _word_count(result.profile)
    if not (PROFILE_WORD_RANGE[0] <= wc <= PROFILE_WORD_RANGE[1]):
        errors.append(f"profile is {wc} words, expected roughly "
                      f"{PROFILE_WORD_RANGE[0]}-{PROFILE_WORD_RANGE[1]}.")

    letter_wc = sum(_word_count(p) for p in result.letter_paragraphs)
    if not (LETTER_WORD_RANGE[0] <= letter_wc <= LETTER_WORD_RANGE[1]):
        errors.append(f"letter_paragraphs total {letter_wc} words, expected roughly "
                      f"{LETTER_WORD_RANGE[0]}-{LETTER_WORD_RANGE[1]}.")
    if not result.letter_paragraphs:
        errors.append("letter_paragraphs is empty.")

    return errors


_FACT_TOKEN_RE = re.compile(r"\b\d{4}\b|\b[A-Z][a-zA-Z]{2,}\b")
# Common structural/salutation words that are capitalized for reasons that
# have nothing to do with being a fact (sentence-initial, greeting, etc.) --
# excluded to keep this check's noise down. Not exhaustive; it only needs to
# cut the most common false positives since this is advisory, not a blocker.
_COMMON_CAPITALIZED_WORDS = {
    "the", "this", "that", "these", "those", "with", "and", "for", "your",
    "dear", "sincerely", "regards", "geachte", "graag", "met", "vriendelijke",
    "groet", "mijn", "mevrouw", "heer", "team", "best", "hello", "hi",
    "january", "february", "march", "april", "may", "june", "july", "august",
    "september", "october", "november", "december",
    "januari", "februari", "maart", "mei", "juni", "juli", "augustus",
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
    "maandag", "dinsdag", "woensdag", "donderdag", "vrijdag", "zaterdag", "zondag",
}


def _facts_text_blob(candidate: dict) -> str:
    """Flatten every true fact (names, employers, degrees, years, skills)
    into one lowercase blob to check free text against. Deliberately
    permissive -- this is a WARNING check, not a blocker, so false positives
    (a common English word that happens to be capitalized) are an acceptable
    cost for never silently approving an invented fact."""
    parts = []
    p = candidate.get("personal", {})
    if isinstance(p, dict):
        parts.append(p.get("name", ""))
    for role in candidate.get("experience", []) or []:
        if not isinstance(role, dict):
            continue
        parts.append(role.get("company", ""))
        parts.append(role.get("title", ""))
        parts.append(role.get("title_en", ""))
        parts.append(role.get("period", ""))
        parts.append(role.get("period_en", ""))
        for b in role.get("bullets", []) or []:
            if isinstance(b, dict):
                parts.append(b.get("nl", ""))
                parts.append(b.get("en", ""))
    for edu in candidate.get("education", []) or []:
        if isinstance(edu, dict):
            parts.extend([edu.get(k, "") for k in
                         ("degree", "degree_en", "school", "school_en", "period", "period_en")])
    for skill in candidate.get("skills", []) or []:
        if isinstance(skill, dict):
            parts.append(skill.get("name", ""))
    parts.extend(candidate.get("highlights", []) or [])
    return " ".join(str(p) for p in parts).lower()


def ground_tailor_result(result: TailorResult, candidate: dict) -> List[str]:
    """Non-blocking warnings: a 4-digit year or a capitalized word/phrase in
    the free text (profile + letter) that doesn't appear anywhere in
    candidate.yaml's facts. Shown on the review page, never blocks
    rendering -- the LLM is instructed not to invent facts, but this is the
    code-side check that catches it if it does anyway."""
    warnings: List[str] = []
    facts_blob = _facts_text_blob(candidate)
    free_text = result.profile + " " + " ".join(result.letter_paragraphs)
    seen = set()
    for match in _FACT_TOKEN_RE.finditer(free_text):
        token = match.group(0)
        key = token.lower()
        if key in seen or key in _COMMON_CAPITALIZED_WORDS:
            continue
        seen.add(key)
        if key not in facts_blob:
            warnings.append(f"{token!r} appears in the generated text but not in "
                            "candidate.yaml's facts -- verify it wasn't invented.")
    return warnings
