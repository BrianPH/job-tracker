"""Issue #10: two-step analyse -> tailor pipeline via the Claude API.

Replaces the single "write a profile paragraph" call (generator.py) with:
  1. analyse_vacancy -- Haiku (ANTHROPIC_MODEL_EXTRACT), extracts requirements/
     keywords/language/contact/deadline/application requirements from the
     vacancy text.
  2. tailor_application -- Sonnet (ANTHROPIC_MODEL_WRITE), given the analysis,
     the candidate's bullet bank, and style examples, produces a structured
     TailorResult: profile paragraph, bullet/skill selection (by id, never new
     text), letter paragraphs, and a requirement -> evidence map.

Both calls use the official `anthropic` SDK's structured outputs
(`client.messages.parse(output_format=...)`), which guarantees the reply
parses into the Pydantic model -- callers still must run
generation_schema.validate_tailor_result() afterwards, since "valid JSON
shape" and "every id actually exists in candidate.yaml" are different checks.

No live call happens in tests (see tests/test_tailor.py) -- every test passes
a fake client whose `messages.parse` is a plain function, matching this
module's dependency-injection point (`client` parameter) rather than
monkeypatching the SDK.
"""
from __future__ import annotations

import os
from typing import Optional

from .generation_schema import AnalysisResult, TailorResult
from .scorer import ANTHROPIC_MODEL_EXTRACT, ANTHROPIC_MODEL_WRITE

ANALYSE_MAX_TOKENS = 1024
TAILOR_MAX_TOKENS = 4096


class GenerationUnavailableError(RuntimeError):
    """No reachable Anthropic client -- e.g. ANTHROPIC_API_KEY is unset. The
    caller falls back to the JSON handoff (issue #11), not handled here."""


class GenerationTruncatedError(RuntimeError):
    """The model's reply was cut off at max_tokens -- treated as a failed
    generation, never rendered, per the issue's validator requirement."""


def make_client():
    """Returns an `anthropic.Anthropic` client, or None if no API key is
    configured. Mirrors scorer.make_caller()'s "no key -> None" convention
    rather than raising, so callers decide how to react."""
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        return None
    import anthropic
    return anthropic.Anthropic(api_key=key)


def _lang_name(lang: str) -> str:
    return "Dutch" if lang == "nl" else "English"


ANALYSE_SYSTEM = (
    "You analyse a job vacancy posting and extract structured facts about it. "
    "Use ONLY what the text states or clearly implies -- never invent a "
    "requirement, deadline, or contact that isn't there.\n"
    "- language: the vacancy's own language, exactly \"nl\" or \"en\".\n"
    "- requirements: concrete must-haves/should-haves the posting states.\n"
    "- keywords: short technical/domain keywords useful for matching a CV "
    "against this vacancy.\n"
    "- contact: a named contact person or department, if given; else empty.\n"
    "- deadline: an application deadline, if stated; else empty.\n"
    "- application_requirements: anything the posting asks for beyond a plain "
    "CV + letter -- a page limit, referees, a code sample, any other explicit "
    "requirement; booleans default to false and lists default to empty when "
    "the text doesn't mention them.\n"
    "Output ONLY the structured result."
)

TAILOR_SYSTEM = (
    "You tailor a CV and motivation letter to a specific vacancy, using ONLY "
    "the facts and bullet bank given to you.\n"
    "HARD RULES:\n"
    "- NEVER invent employers, projects, degrees, skills, years of "
    "experience, or certifications. Every claim must trace to a given fact "
    "or bullet.\n"
    "- `experience_order` and the keys of `bullets` must be role ids from the "
    "bullet bank, verbatim. `bullets` values must be bullet ids from that "
    "SAME role, verbatim -- you select and order existing bullets, you never "
    "write new bullet text.\n"
    "- `skills` is an ordered subset of the given skill names, verbatim.\n"
    "- Write `profile` and `letter_paragraphs` in {language}. `profile` is "
    "55-85 words. `letter_paragraphs` is 4-6 paragraphs, 250-400 words total, "
    "with no salutation (Dear.../Geachte...) or closing (Sincerely/Met "
    "vriendelijke groet) -- those are added separately. Match the tone and "
    "structure of the style example, but write fresh text connecting the "
    "candidate's real experience to what THIS vacancy asks for.\n"
    "- `requirement_map`: for each vacancy requirement, list the bullet/fact "
    "ids that address it and a status of \"met\", \"partial\", or \"gap\". "
    "This is shown only to the candidate for review, never sent to the "
    "employer -- an honest \"gap\" is more useful than a forced \"met\".\n"
    "- `notes_for_candidate`: anything the candidate should know before "
    "applying (e.g. \"asks for two referees\", \"deadline is in 3 days\").\n"
    "Output ONLY the structured result."
)


def _bullet_bank_text(candidate: dict, lang: str) -> str:
    lines = []
    for role in candidate.get("experience", []) or []:
        if not isinstance(role, dict) or not role.get("id"):
            continue
        title = (role.get("title_en") if lang == "en" else role.get("title")) \
            or role.get("title", "")
        period = (role.get("period_en") if lang == "en" else role.get("period")) \
            or role.get("period", "")
        lines.append(f'ROLE id={role["id"]} title="{title}" company="{role.get("company","")}" '
                    f'period="{period}"')
        for bullet in role.get("bullets", []) or []:
            if not isinstance(bullet, dict) or not bullet.get("id"):
                continue
            text = bullet.get(lang) or bullet.get("nl") or bullet.get("en") or ""
            tags = ", ".join(bullet.get("tags") or [])
            lines.append(f'  BULLET id={bullet["id"]} tags=[{tags}]: "{text}"')
    return "\n".join(lines)


def _skills_text(candidate: dict) -> str:
    return ", ".join(s.get("name", "") for s in (candidate.get("skills") or [])
                     if isinstance(s, dict) and s.get("name"))


def analyse_vacancy(vacancy_text: str, client=None, model: Optional[str] = None) -> AnalysisResult:
    """Haiku call: vacancy text -> structured AnalysisResult."""
    client = client if client is not None else make_client()
    if client is None:
        raise GenerationUnavailableError("No Anthropic client available (ANTHROPIC_API_KEY unset).")
    model = model or os.getenv("ANTHROPIC_MODEL_EXTRACT", ANTHROPIC_MODEL_EXTRACT)

    response = client.messages.parse(
        model=model,
        max_tokens=ANALYSE_MAX_TOKENS,
        system=ANALYSE_SYSTEM,
        messages=[{"role": "user", "content": f"VACANCY TEXT:\n{vacancy_text}"}],
        output_format=AnalysisResult,
    )
    if response.stop_reason == "max_tokens":
        raise GenerationTruncatedError(
            f"Analyse reply truncated at max_tokens={ANALYSE_MAX_TOKENS} (model {model}).")
    return response.parsed_output


def tailor_application(job: dict, analysis: AnalysisResult, candidate: dict,
                       style_example: str = "", client=None,
                       model: Optional[str] = None) -> TailorResult:
    """Sonnet call: analysis + bullet bank + style example -> structured
    TailorResult. `style_example` is prior letters by the same candidate
    (e.g. generator.example_letter_text(analysis.language)) -- optional
    since not every checkout has one."""
    client = client if client is not None else make_client()
    if client is None:
        raise GenerationUnavailableError("No Anthropic client available (ANTHROPIC_API_KEY unset).")
    model = model or os.getenv("ANTHROPIC_MODEL_WRITE", ANTHROPIC_MODEL_WRITE)
    lang = analysis.language

    user = (
        f"VACANCY:\nTitle: {job.get('title','')}\nCompany: {job.get('company','')}\n"
        f"Location: {job.get('location','')}\n"
        f"Description: {job.get('description','')}\n\n"
        f"ANALYSIS:\nRequirements: {', '.join(analysis.requirements)}\n"
        f"Keywords: {', '.join(analysis.keywords)}\n\n"
        f"BULLET BANK (role id / bullet id -- select and order from these ONLY):\n"
        f"{_bullet_bank_text(candidate, lang)}\n\n"
        f"SKILLS (select an ordered subset, verbatim):\n{_skills_text(candidate)}\n\n"
        f"STYLE EXAMPLE (previous letters by the same candidate):\n{style_example}\n\n"
        f"Write the tailored CV/letter result now, in {_lang_name(lang)}."
    )
    system = TAILOR_SYSTEM.replace("{language}", _lang_name(lang))

    response = client.messages.parse(
        model=model,
        max_tokens=TAILOR_MAX_TOKENS,
        system=system,
        messages=[{"role": "user", "content": user}],
        output_format=TailorResult,
    )
    if response.stop_reason == "max_tokens":
        raise GenerationTruncatedError(
            f"Tailor reply truncated at max_tokens={TAILOR_MAX_TOKENS} (model {model}).")
    return response.parsed_output
