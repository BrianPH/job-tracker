"""issue #11/M3b, extended by issue #12: orchestrates the analyse -> tailor
pipeline (jobtracker/tailor.py, issue #10) for one job.

generate_or_handoff() and apply_handoff_upload() produce a DRAFT -- a
TailorResult plus enough context (analysis, candidate, style_example) to
regenerate it -- for a human to review before anything is written to disk.
Neither renders a document directly: "nothing is sent to an employer
unseen" (issue #12) means a review step always sits between generation and
the final .docx. jobtracker/webapp.py stores the draft (its DRAFTS dict)
and exposes it on a review page; regenerate() re-runs just the tailor step
against edits/a fresh attempt, and render_draft() is the only function in
this module that actually writes .docx files, applying whatever the review
page's edits (profile text, letter paragraphs, bullet selection) were.

On any unrecovered API failure (no key, no credit, truncated/invalid reply),
all three entry points write a JSON handoff request instead of guessing --
see write_handoff_request below. apply_handoff_upload() is the other side
of that handoff: a human ran .claude/commands/tailor.md (or pasted
request.json into claude.ai) and uploads the resulting generation.json,
which also becomes a draft rather than rendering directly, so the live and
handoff paths land on the same review step.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import anthropic
from pydantic import ValidationError

from . import generator, tailor
from .generation_schema import AnalysisResult, TailorResult, validate_tailor_result

OUTPUT_DIR = generator.OUTPUT_DIR
HANDOFF_STATUS = "Wacht op generatie"

# Exceptions that mean "this attempt failed, write the JSON handoff" rather
# than a bug in this process. Rate-limit/overloaded/network failures are
# retried by the SDK itself (tailor.make_client's max_retries=3) before
# they ever reach here -- anything surviving that, plus the pipeline's own
# unavailable/truncated/credit-balance types, lands in this tuple. Anything
# NOT in this tuple (a real bug -- KeyError, AttributeError, ...) propagates
# uncaught, same as any other unexpected exception.
_HANDOFF_EXCEPTIONS = (
    tailor.GenerationUnavailableError,
    tailor.GenerationTruncatedError,
    tailor.CreditBalanceError,
    anthropic.APIStatusError,
    anthropic.APIConnectionError,
)


def _request_path(job: dict, candidate: dict, lang: str, out_dir: Path) -> Path:
    cv_path, _ = generator.application_paths(job, candidate, lang, out_dir)
    return cv_path.parent / "request.json"


def _saved_previous_status(path: Path) -> Optional[str]:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("previous_status")
    except (OSError, json.JSONDecodeError):
        return None


def _previous_status(job: dict, path: Path) -> str:
    """The status to restore once a handoff is resolved. Usually just the
    job's current status at the moment of writing request.json -- but if
    this is a SECOND consecutive failure (a prior handoff that was retried
    and failed again before ever being resolved), job["status"] is already
    HANDOFF_STATUS, which would overwrite -- and lose -- the real original.
    Fall back to whatever the existing request.json already recorded."""
    current = job.get("status") or "New"
    if current != HANDOFF_STATUS:
        return current
    return _saved_previous_status(path) or "New"


def write_handoff_request(job: dict, candidate: dict, lang: str, error: str,
                          analysis: Optional[object] = None, style_example: str = "",
                          out_dir: Path = OUTPUT_DIR,
                          set_status: Optional[Callable[[str], None]] = None) -> Path:
    """Writes applications/<Company>_<Title>/request.json and flips the job
    status to HANDOFF_STATUS -- everything `.claude/commands/tailor.md`
    needs to reproduce the tailor call by hand and get back a
    generation.json that renders through the exact same path a live call
    would (apply_handoff_upload, below)."""
    path = _request_path(job, candidate, lang, out_dir)
    payload = {
        # job_id is included so .claude/commands/tailor.md can find this
        # file by searching applications/*/request.json rather than having
        # to re-derive the folder name -- an ADHOC_JOBS job (ingested from a
        # URL/paste) exists only in the webapp's memory, not in any store a
        # separate Claude Code session could query by job_id.
        "job": {
            "job_id": job.get("job_id", ""),
            "title": job.get("title", ""), "company": job.get("company", ""),
            "location": job.get("location", ""),
            # capped the same way the live analyse/tailor prompts cap it
            # (tailor.VACANCY_DESC_CHARS), so a hand-run /tailor sees
            # exactly what the live call would have -- not more.
            "description": (job.get("description") or "")[:tailor.VACANCY_DESC_CHARS],
        },
        "analysis": analysis.model_dump() if analysis is not None else None,
        "error": error,
        # restored by _clear_handoff once this handoff is resolved, rather
        # than hardcoding "New" and clobbering a status the user had
        # already set (e.g. "Reviewing") before generation failed.
        "previous_status": _previous_status(job, path),
        **tailor.handoff_context(candidate, lang, style_example),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    if set_status is not None:
        set_status(HANDOFF_STATUS)
    return path


def _clear_handoff(job: dict, candidate: dict, lang: str, out_dir: Path,
                   set_status: Optional[Callable[[str], None]]) -> None:
    """Restores whatever status the job had before a handoff, once that
    handoff is resolved by either a live retry or an upload succeeding. A
    no-op when the job was never in the handoff state -- the ordinary
    generate-again-on-an-already-rendered-job path must not touch status
    at all."""
    if set_status is None or job.get("status") != HANDOFF_STATUS:
        return
    path = _request_path(job, candidate, lang, out_dir)
    set_status(_saved_previous_status(path) or "New")


def _render(candidate: dict, result: TailorResult, job: dict, lang: str, out_dir: Path) -> dict:
    cv_path, letter_path = generator.application_paths(job, candidate, lang, out_dir)
    cv_ctx = generator.cv_render_context_from_tailor_result(candidate, result, lang)
    letter_ctx = generator.letter_render_context(candidate, job, result.letter_paragraphs, lang)
    generator.build_cv_docx(cv_ctx, lang, cv_path)
    generator.build_letter_docx(letter_ctx, lang, letter_path)
    return {"state": "done", "cv": cv_path, "letter": letter_path, "lang": lang}


def generate_or_handoff(job: dict, candidate: Optional[dict] = None,
                        out_dir: Path = OUTPUT_DIR, client=None,
                        set_status: Optional[Callable[[str], None]] = None) -> dict:
    """The ONLY generation entry point jobtracker/webapp.py calls. Returns
    a DRAFT on success -- {"state": "draft", "job":..., "candidate":...,
    "analysis":..., "lang": str, "style_example": str, "result":
    TailorResult} -- for review (issue #12), never a rendered document; or
    {"state": "handoff", "request_path": Path, "error": str} on any
    unrecovered failure."""
    candidate = candidate if candidate is not None else generator.load_candidate()
    lang_guess = generator.detect_language(f"{job.get('title','')} {job.get('description','')}")

    try:
        analysis = tailor.analyse_vacancy(job.get("description", ""), client=client)
    except _HANDOFF_EXCEPTIONS as e:
        path = write_handoff_request(job, candidate, lang_guess, str(e),
                                     analysis=None, out_dir=out_dir, set_status=set_status)
        return {"state": "handoff", "request_path": path, "error": str(e)}

    style_example = generator.example_letter_text(analysis.language)
    try:
        result, errors = tailor.run_tailor_pipeline(job, analysis, candidate, style_example,
                                                     client=client)
    except _HANDOFF_EXCEPTIONS as e:
        path = write_handoff_request(job, candidate, analysis.language, str(e),
                                     analysis=analysis, style_example=style_example,
                                     out_dir=out_dir, set_status=set_status)
        return {"state": "handoff", "request_path": path, "error": str(e)}

    if errors:
        error_msg = "Tailor result failed validation after one retry: " + "; ".join(errors)
        path = write_handoff_request(job, candidate, analysis.language, error_msg,
                                     analysis=analysis, style_example=style_example,
                                     out_dir=out_dir, set_status=set_status)
        return {"state": "handoff", "request_path": path, "error": error_msg}

    _clear_handoff(job, candidate, analysis.language, out_dir, set_status)
    return {"state": "draft", "job": job, "candidate": candidate, "analysis": analysis,
           "lang": analysis.language, "style_example": style_example, "result": result}


def _load_handoff_context(job: dict, candidate: dict, lang: str,
                          out_dir: Path) -> Tuple[Optional[AnalysisResult], str]:
    """Best-effort recovery of the AnalysisResult + style_example that were
    recorded in request.json when the handoff was written, so a draft that
    came from an upload can still use "Regenerate" (which needs the SAME
    analysis, not a re-run of the analyse step -- issue #12's acceptance
    criteria). Falls back to (None, "") if request.json is missing or
    unreadable, or if the analyse step itself had failed before the
    handoff was written (analysis is null in that case too) -- the upload
    and render still work either way, "Regenerate" just isn't available
    for that draft."""
    path = _request_path(job, candidate, lang, out_dir)
    if not path.is_file():
        return None, ""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, ""
    analysis_data = payload.get("analysis")
    analysis = AnalysisResult.model_validate(analysis_data) if analysis_data else None
    return analysis, payload.get("style_example", "")


def apply_handoff_upload(job: dict, generation_json: dict, candidate: Optional[dict] = None,
                         out_dir: Path = OUTPUT_DIR,
                         set_status: Optional[Callable[[str], None]] = None) -> dict:
    """A human ran `.claude/commands/tailor.md` (or pasted request.json into
    claude.ai) and uploads the resulting generation.json -- a bare
    TailorResult JSON object, no wrapper. Goes through the EXACT SAME
    validator as a live reply; on success this becomes a draft (same as
    generate_or_handoff's success path -- issue #12: the live and handoff
    paths land on the same review step) and restores whatever status the
    job had before the handoff. On failure it renders nothing and returns
    the validation errors -- never a partial/best-effort document."""
    candidate = candidate if candidate is not None else generator.load_candidate()
    try:
        result = TailorResult.model_validate(generation_json)
    except ValidationError as e:
        return {"state": "invalid", "errors": [str(e)]}

    errors = validate_tailor_result(result, candidate)
    if errors:
        return {"state": "invalid", "errors": errors}

    analysis, style_example = _load_handoff_context(job, candidate, result.language, out_dir)
    _clear_handoff(job, candidate, result.language, out_dir, set_status)
    return {"state": "draft", "job": job, "candidate": candidate, "analysis": analysis,
           "lang": result.language, "style_example": style_example, "result": result}


def regenerate(job: dict, analysis: AnalysisResult, candidate: dict, style_example: str = "",
               out_dir: Path = OUTPUT_DIR, client=None,
               set_status: Optional[Callable[[str], None]] = None) -> dict:
    """issue #12: re-runs ONLY the tailor step -- the acceptance criteria's
    "without re-running the ingest/analyse steps" -- a single
    `tailor_application` call, no automatic validator-retry (confirmed
    decision: a human is already about to review the output, so retrying
    invisibly before they see anything isn't worth another API call).
    Returns {"state": "draft", "result": TailorResult, "errors": [...]}
    on a structural success; `errors` is ADVISORY here, for the review
    page to show -- not a reason to discard the attempt, since the review
    page's bullet checkboxes are drawn from the real bullet bank, so a
    stale/invented id in the reply just doesn't show up checked rather
    than crashing or needing to be rejected outright. Only an SDK-level
    failure is treated as a real failure, written to the JSON handoff like
    any other unrecovered one."""
    client = client if client is not None else tailor.make_client()
    try:
        result = tailor.tailor_application(job, analysis, candidate, style_example, client=client)
    except _HANDOFF_EXCEPTIONS as e:
        path = write_handoff_request(job, candidate, analysis.language, str(e),
                                     analysis=analysis, style_example=style_example,
                                     out_dir=out_dir, set_status=set_status)
        return {"state": "handoff", "request_path": path, "error": str(e)}
    errors = validate_tailor_result(result, candidate)
    return {"state": "draft", "result": result, "errors": errors}


def render_draft(draft: dict, profile: str, letter_paragraphs: List[str],
                 bullets: Dict[str, List[str]], out_dir: Path = OUTPUT_DIR) -> dict:
    """issue #12: the only function in this module that writes .docx files.
    Applies the review page's edits -- `profile`/`letter_paragraphs` free
    text, `bullets` (role id -> ordered bullet ids) from checked checkboxes
    -- onto a copy of the draft's TailorResult and renders. No
    re-validation here: the review page's bullet checkboxes are drawn from
    the real bullet bank (so `bullets` can't reference an id that doesn't
    exist), and the free-text fields have no id-validity concept to check.
    `experience_order`/`skills` are left exactly as the draft's result had
    them -- the review page doesn't offer reordering roles or skills,
    only which bullets render within the roles the model already picked."""
    result = draft["result"].model_copy(update={
        "profile": profile, "letter_paragraphs": letter_paragraphs, "bullets": bullets,
    })
    return _render(draft["candidate"], result, draft["job"], draft["lang"], out_dir)
