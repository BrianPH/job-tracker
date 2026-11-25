"""issue #11/M3b: orchestrates the analyse -> tailor pipeline
(jobtracker/tailor.py, issue #10) for one job and renders the result via
jobtracker/generator.py's docxtpl pieces -- or, on any unrecovered failure,
writes a JSON handoff request instead of the silent template fallback the
old generator.generate_application had.

jobtracker/webapp.py calls generate_or_handoff() as its ONLY generation
entry point. apply_handoff_upload() is the other side of the handoff: a
human ran .claude/commands/tailor.md (or pasted request.json into
claude.ai) and uploads the resulting generation.json.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Callable, Optional

import anthropic
from pydantic import ValidationError

from . import generator, tailor
from .generation_schema import TailorResult, validate_tailor_result

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
        "job": {
            "title": job.get("title", ""), "company": job.get("company", ""),
            "location": job.get("location", ""), "description": job.get("description", ""),
        },
        "analysis": analysis.model_dump() if analysis is not None else None,
        "error": error,
        **tailor.handoff_context(candidate, lang, style_example),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    if set_status is not None:
        set_status(HANDOFF_STATUS)
    return path


def _render(candidate: dict, result: TailorResult, job: dict, lang: str, out_dir: Path) -> dict:
    cv_path, letter_path = generator.application_paths(job, candidate, lang, out_dir)
    cv_ctx = generator.cv_render_context_from_tailor_result(candidate, result, lang)
    letter_body = "\n\n".join(result.letter_paragraphs)
    letter_ctx = generator.letter_render_context(candidate, job, letter_body, lang)
    generator.build_cv_docx(cv_ctx, lang, cv_path)
    generator.build_letter_docx(letter_ctx, lang, letter_path)
    return {"state": "done", "cv": cv_path, "letter": letter_path, "lang": lang}


def generate_or_handoff(job: dict, candidate: Optional[dict] = None,
                        out_dir: Path = OUTPUT_DIR, client=None,
                        set_status: Optional[Callable[[str], None]] = None) -> dict:
    """The ONLY generation entry point jobtracker/webapp.py calls. Returns
    {"state": "done", "cv": Path, "letter": Path, "lang": str} on success,
    or {"state": "handoff", "request_path": Path, "error": str} on any
    unrecovered failure -- never renders a silent fallback document."""
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
        if errors:
            raise tailor.GenerationTruncatedError(
                "Tailor result failed validation after one retry: " + "; ".join(errors))
    except _HANDOFF_EXCEPTIONS as e:
        path = write_handoff_request(job, candidate, analysis.language, str(e),
                                     analysis=analysis, style_example=style_example,
                                     out_dir=out_dir, set_status=set_status)
        return {"state": "handoff", "request_path": path, "error": str(e)}

    return _render(candidate, result, job, analysis.language, out_dir)


def apply_handoff_upload(job: dict, generation_json: dict, candidate: Optional[dict] = None,
                         out_dir: Path = OUTPUT_DIR,
                         set_status: Optional[Callable[[str], None]] = None) -> dict:
    """A human ran `.claude/commands/tailor.md` (or pasted request.json into
    claude.ai) and uploads the resulting generation.json -- a bare
    TailorResult JSON object, no wrapper. Goes through the EXACT SAME
    validator as a live reply; on success this renders (same as
    generate_or_handoff's success path) and clears the handoff status back
    to "New" for re-review. On failure it renders nothing and returns the
    validation errors -- never a partial/best-effort document."""
    candidate = candidate if candidate is not None else generator.load_candidate()
    try:
        result = TailorResult.model_validate(generation_json)
    except ValidationError as e:
        return {"state": "invalid", "errors": [str(e)]}

    errors = validate_tailor_result(result, candidate)
    if errors:
        return {"state": "invalid", "errors": errors}

    rendered = _render(candidate, result, job, result.language, out_dir)
    if set_status is not None:
        set_status("New")
    return rendered
