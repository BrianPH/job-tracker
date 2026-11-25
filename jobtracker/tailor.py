"""Issue #10: two-step analyse -> tailor pipeline via the Claude API.

Orchestrated by jobtracker/generation.py (issue #11/M3b), which
jobtracker/webapp.py calls as its only generation entry point:
  1. analyse_vacancy -- Haiku (ANTHROPIC_MODEL_EXTRACT), extracts requirements/
     keywords/language/contact/deadline/application requirements from the
     vacancy text.
  2. tailor_application -- Sonnet (ANTHROPIC_MODEL_WRITE), given the analysis,
     the candidate's bullet bank, and style examples, produces a structured
     TailorResult: profile paragraph, bullet/skill selection (by id, never new
     text), letter paragraphs, and a requirement -> evidence map.

Both calls use the official `anthropic` SDK's structured outputs
(`client.messages.parse(output_format=...)`): a complete reply is guaranteed
to parse into the Pydantic model, but truncation (see GenerationTruncatedError)
and the SDK's own exceptions (auth, rate limit, refusal) still apply. Callers
still must run generation_schema.validate_tailor_result() afterwards even on
a successful parse, since "valid JSON shape" and "every id actually exists in
candidate.yaml" are different checks.

No live call happens in tests (see tests/test_tailor.py) -- every test passes
a fake client whose `messages.parse` is a plain function, matching this
module's dependency-injection point (`client` parameter) rather than
monkeypatching the SDK.
"""
from __future__ import annotations

import os
from typing import List, Optional, Tuple

from pydantic import ValidationError

from .generation_schema import AnalysisResult, TailorResult, validate_tailor_result
from .scorer import ANTHROPIC_MODEL_EXTRACT, ANTHROPIC_MODEL_WRITE

# Only the generic `html` adapter caps description length on its own (at
# 4000 chars, jobtracker/adapters.py); JSON-API adapters and ingest.py pass
# descriptions through uncapped. Cap here too, at the same size (matching
# the WVN_DUTIES_CHARS/WVN_REQUIREMENTS_CHARS split in adapters.py), so an
# unusually long posting can't blow the model's context window or run up
# Anthropic spend unbounded -- applied to both the analyse and tailor
# prompts, since both embed the full vacancy description.
VACANCY_DESC_CHARS = 4000

ANALYSE_MAX_TOKENS = 1024
TAILOR_MAX_TOKENS = 8192
# Sonnet 5 runs adaptive thinking by default when `thinking` is omitted, and
# max_tokens caps thinking + the structured JSON reply together -- confirmed
# against the live API (issue #12's manual end-to-end check) truncating a
# real TailorResult reply right at the end of the JSON (stop_reason never
# even reached "max_tokens": the SDK's own JSON parse failed first, on
# incomplete JSON) well under TAILOR_MAX_TOKENS, because thinking consumed
# a large, variable share of the budget first. Neither call benefits from
# extended reasoning -- both are constrained extraction/selection tasks
# governed by hard rules in the prompt, not open-ended problems -- so
# thinking is disabled below rather than padding the budget further and
# hoping it's enough.
THINKING_DISABLED = {"type": "disabled"}


class GenerationUnavailableError(RuntimeError):
    """No reachable Anthropic client -- neither ANTHROPIC_API_KEY nor
    ANTHROPIC_AUTH_TOKEN is set. The caller falls back to the JSON handoff
    (issue #11), not handled here."""


class CreditBalanceError(RuntimeError):
    """The account has run out of API credit -- `anthropic.PaymentRequiredError`
    (HTTP 402). Raised instead of letting the SDK exception propagate so
    jobtracker/generation.py's handoff path can tell a human "top up and
    retry" apart from a generic API failure. Earlier drafts of this feature
    planned to detect this by matching "credit balance" in the error
    message, but the SDK's own `APIStatusError.api_message` docstring now
    says "Its wording can change, so don't match on it" and exposes a
    dedicated 402 exception type instead -- used here rather than text
    matching."""


class GenerationTruncatedError(RuntimeError):
    """The model's reply was truncated or otherwise failed to parse against
    the schema -- treated as a failed generation, never rendered, per the
    issue's validator requirement.

    A genuinely truncated reply is incomplete JSON, so `messages.parse()`
    fails Pydantic validation while building the response -- the SDK never
    gets as far as returning an object whose `.stop_reason` we could read.
    The `_parse_or_raise` helper below catches that `pydantic.ValidationError`
    and raises this instead. Note this is a superset of truncation: the same
    ValidationError also fires if the model emits a value a Literal field
    rejects (e.g. an out-of-enum status) -- that reply isn't truncated, just
    schema-invalid, but the fallback (issue #11) is the same either way, so
    one exception covers both. The `stop_reason == "max_tokens"` check in the
    functions below is kept too, in case a reply is cut exactly on a valid
    JSON boundary and so parses despite being marked truncated."""


def make_client():
    """Returns an `anthropic.Anthropic` client, or None if no credential is
    configured. Mirrors scorer.make_caller()'s "no key -> None" convention
    rather than raising, so callers decide how to react. Checks both
    ANTHROPIC_API_KEY and ANTHROPIC_AUTH_TOKEN (the SDK's two static
    credential env vars); a host authenticated only via an `ant auth login`
    OAuth profile or workload-identity federation isn't detected here, since
    the SDK only resolves those at request time, not at construction --
    set one of the two env vars explicitly if that matters for a deployment."""
    key = os.getenv("ANTHROPIC_API_KEY")
    auth_token = os.getenv("ANTHROPIC_AUTH_TOKEN")
    if not key and not auth_token:
        return None
    import anthropic
    # Rate-limit/overloaded/network failures are retried by the SDK itself
    # (issue #11/M3b) rather than hand-rolled here -- only a failure that
    # survives these retries reaches the JSON handoff.
    if key:
        return anthropic.Anthropic(api_key=key, max_retries=3)
    return anthropic.Anthropic(auth_token=auth_token, max_retries=3)


def _parse_or_raise(client, **kwargs):
    """Wraps `client.messages.parse(**kwargs)`: a truncated reply is
    incomplete JSON, which fails Pydantic validation inside the SDK before
    it ever returns -- translate that into GenerationTruncatedError so
    callers have one error to handle instead of needing to know this SDK
    detail. `anthropic.PaymentRequiredError` (402, no credit left) is
    translated into CreditBalanceError so the handoff path (issue #11) can
    tag that case distinctly. Any other exception (network, auth, rate
    limit) propagates unchanged; callers are expected to catch anthropic's
    own exception types (e.g. APIConnectionError, AuthenticationError) for
    those -- the SDK's own max_retries (see make_client) already absorbs
    rate-limit/overloaded/network failures before they get here."""
    import anthropic
    try:
        return client.messages.parse(**kwargs)
    except anthropic.PaymentRequiredError as e:
        raise CreditBalanceError(
            f"Anthropic account has no API credit left (model {kwargs.get('model')}).") from e
    except ValidationError as e:
        raise GenerationTruncatedError(
            f"Reply did not parse against the schema -- truncated at "
            f"max_tokens={kwargs.get('max_tokens')} or schema-invalid (model "
            f"{kwargs.get('model')})."
        ) from e


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
    "- `requirement_map`: for each vacancy requirement, list the role, "
    "bullet, education, or skill ids (verbatim, from the lists given to you) "
    "that address it, and a status of \"met\", \"partial\", or \"gap\". A "
    "degree requirement should point at the education id directly, not a "
    "bullet. This is shown only to the candidate for review, never sent to "
    "the employer -- an honest \"gap\" is more useful than a forced \"met\".\n"
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


def _education_text(candidate: dict, lang: str) -> str:
    lines = []
    for edu in candidate.get("education", []) or []:
        if not isinstance(edu, dict) or not edu.get("id"):
            continue
        degree = (edu.get("degree_en") if lang == "en" else edu.get("degree")) \
            or edu.get("degree", "")
        school = (edu.get("school_en") if lang == "en" else edu.get("school")) \
            or edu.get("school", "")
        period = (edu.get("period_en") if lang == "en" else edu.get("period")) \
            or edu.get("period", "")
        lines.append(f'EDUCATION id={edu["id"]} degree="{degree}" school="{school}" '
                    f'period="{period}"')
    return "\n".join(lines)


def analyse_vacancy(vacancy_text: str, client=None, model: Optional[str] = None) -> AnalysisResult:
    """Haiku call: vacancy text -> structured AnalysisResult."""
    client = client if client is not None else make_client()
    if client is None:
        raise GenerationUnavailableError("No Anthropic client available (ANTHROPIC_API_KEY and ANTHROPIC_AUTH_TOKEN both unset).")
    model = model or os.getenv("ANTHROPIC_MODEL_EXTRACT", ANTHROPIC_MODEL_EXTRACT)

    response = _parse_or_raise(
        client,
        model=model,
        max_tokens=ANALYSE_MAX_TOKENS,
        thinking=THINKING_DISABLED,
        system=ANALYSE_SYSTEM,
        messages=[{"role": "user",
                   "content": f"VACANCY TEXT:\n{(vacancy_text or '')[:VACANCY_DESC_CHARS]}"}],
        output_format=AnalysisResult,
    )
    if response.stop_reason == "max_tokens":
        raise GenerationTruncatedError(
            f"Analyse reply truncated at max_tokens={ANALYSE_MAX_TOKENS} (model {model}).")
    return response.parsed_output


def tailor_application(job: dict, analysis: AnalysisResult, candidate: dict,
                       style_example: str = "", client=None,
                       model: Optional[str] = None,
                       retry_errors: Optional[List[str]] = None) -> TailorResult:
    """Sonnet call: analysis + bullet bank + style example -> structured
    TailorResult. `style_example` is prior letters by the same candidate
    (e.g. generator.example_letter_text(analysis.language)) -- optional
    since not every checkout has one. `retry_errors` (issue #11/M3b) is
    `generation_schema.validate_tailor_result`'s blocking-error list from a
    PRIOR call against this same vacancy -- appended to the prompt so the
    model can see exactly which ids it got wrong, rather than retrying blind."""
    client = client if client is not None else make_client()
    if client is None:
        raise GenerationUnavailableError("No Anthropic client available (ANTHROPIC_API_KEY and ANTHROPIC_AUTH_TOKEN both unset).")
    model = model or os.getenv("ANTHROPIC_MODEL_WRITE", ANTHROPIC_MODEL_WRITE)
    lang = analysis.language

    user = (
        f"VACANCY:\nTitle: {job.get('title','')}\nCompany: {job.get('company','')}\n"
        f"Location: {job.get('location','')}\n"
        f"Description: {(job.get('description') or '')[:VACANCY_DESC_CHARS]}\n\n"
        f"ANALYSIS:\nRequirements: {', '.join(analysis.requirements)}\n"
        f"Keywords: {', '.join(analysis.keywords)}\n\n"
        f"BULLET BANK (role id / bullet id -- select and order from these ONLY):\n"
        f"{_bullet_bank_text(candidate, lang)}\n\n"
        f"EDUCATION (cite the education id directly for a degree requirement):\n"
        f"{_education_text(candidate, lang)}\n\n"
        f"SKILLS (select an ordered subset, verbatim):\n{_skills_text(candidate)}\n\n"
        f"STYLE EXAMPLE (previous letters by the same candidate):\n{style_example}\n\n"
        f"Write the tailored CV/letter result now, in {_lang_name(lang)}."
    )
    if retry_errors:
        user += (
            "\n\nYour previous reply failed validation for these reasons -- "
            "fix them, using only ids/names from the lists above:\n" +
            "\n".join(f"- {e}" for e in retry_errors)
        )
    system = TAILOR_SYSTEM.replace("{language}", _lang_name(lang))

    response = _parse_or_raise(
        client,
        model=model,
        max_tokens=TAILOR_MAX_TOKENS,
        thinking=THINKING_DISABLED,
        system=system,
        messages=[{"role": "user", "content": user}],
        output_format=TailorResult,
    )
    if response.stop_reason == "max_tokens":
        raise GenerationTruncatedError(
            f"Tailor reply truncated at max_tokens={TAILOR_MAX_TOKENS} (model {model}).")
    return response.parsed_output


def run_tailor_pipeline(job: dict, analysis: AnalysisResult, candidate: dict,
                        style_example: str = "", client=None,
                        model: Optional[str] = None) -> Tuple[TailorResult, List[str]]:
    """issue #11/M3b: `tailor_application` plus ONE retry when the reply is
    structurally valid JSON but fails `generation_schema.validate_tailor_result`
    (an id that doesn't exist in candidate.yaml) -- a different failure mode
    from GenerationTruncatedError, which only covers incomplete/schema-invalid
    JSON. Returns (result, remaining_errors); a non-empty second element
    means the caller should treat this as a failed generation (JSON handoff),
    same as any other unrecovered failure -- this function itself never
    raises on a validation failure, only on an SDK/truncation error from
    `tailor_application`."""
    client = client if client is not None else make_client()

    result = tailor_application(job, analysis, candidate, style_example,
                                client=client, model=model)
    errors = validate_tailor_result(result, candidate)
    if errors:
        result = tailor_application(job, analysis, candidate, style_example,
                                    client=client, model=model, retry_errors=errors)
        errors = validate_tailor_result(result, candidate)
    return result, errors


def handoff_context(candidate: dict, lang: str, style_example: str = "") -> dict:
    """Everything `.claude/commands/tailor.md` needs to reproduce the tailor
    call by hand (issue #11/M3b), built from the SAME prompt pieces the live
    API path uses -- a hand-run call asks the model exactly what the live
    path would have, not a re-derived approximation."""
    return {
        "language": lang,
        "analyse_system_prompt": ANALYSE_SYSTEM,
        "tailor_system_prompt": TAILOR_SYSTEM.replace("{language}", _lang_name(lang)),
        "bullet_bank": _bullet_bank_text(candidate, lang),
        "education": _education_text(candidate, lang),
        "skills": _skills_text(candidate),
        "style_example": style_example,
        "response_schema": TailorResult.model_json_schema(),
    }


def allowed_terms(job: dict, analysis: AnalysisResult) -> list:
    """Terms to pass as `generation_schema.ground_tailor_result`'s
    `extra_allowed_terms` -- legitimately not a candidate fact, but also not
    invented, since TAILOR_SYSTEM explicitly tells the model to connect the
    candidate's experience to this vacancy's company and keywords. Used by
    the review page (issue #12), confirmed live against a real vacancy: a
    multi-word keyword (e.g. "Azure DevOps") is also split into its
    individual words, since `ground_tailor_result` checks one capitalized
    token at a time -- without this, every word of a multi-word keyword
    the letter legitimately echoes back flags as an unverified fact."""
    terms = [job.get("company", "")]
    terms.extend(analysis.keywords)
    terms = [t for t in terms if t]
    words = [w for t in terms for w in t.split() if len(t.split()) > 1]
    return terms + words
