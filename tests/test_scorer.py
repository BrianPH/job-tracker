"""Offline tests for the persona-based LLM scorer: prompt building, response
parsing, backend selection, and assignment. Fake caller — no network, no server.
Run:  python -m tests.test_scorer
"""
import os
import sys
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import jobtracker.scorer as scorer_mod
from jobtracker.models import Job
from jobtracker.scorer import (ANTHROPIC_MODEL_EXTRACT, ANTHROPIC_MODEL_WRITE,
                               build_job_prompt, get_personas, parse_score,
                               score_jobs, make_caller)

PROFILE = {
    "personas": [
        {"name": "Bioinformatics", "summary": "Python, NGS pipelines, junior."},
        {"name": "Cloud / DevOps Engineer", "summary": "CI/CD, Docker, Linux, Azure."},
    ],
    "persona_constraints": "Early-career (0-3 years); senior roles are a weak fit.",
    "profile": {"summary": "Bioinformatician, Python, NGS pipelines."},
    "rules": {"keywords_any": ["bioinformatics", "genomics"]},
}
JOBS = [
    Job(title="Bioinformatics Scientist", company="GenomeScan", source="Recruitee",
        description="Build NGS pipelines in Python.", job_id="a1"),
    Job(title="CI/CD Engineer", company="FinTechCo", source="Greenhouse",
        description="Maintain CI/CD pipelines and Kubernetes clusters.", job_id="b2"),
]


def test_personas_in_prompt():
    system, user = build_job_prompt(PROFILE, JOBS[1])
    assert "persona" in system.lower()
    assert "only ONE" in user or "only one" in user.lower()
    assert "Bioinformatics" in user and "Cloud / DevOps Engineer" in user
    assert "Early-career" in user                 # constraints included
    assert "CI/CD Engineer" in user and "FinTechCo" in user
    print("PASS test_personas_in_prompt")


def test_get_personas_fallback():
    assert len(get_personas(PROFILE)) == 2
    assert get_personas({})[0]["name"] == "Generalist"   # no personas configured
    print("PASS test_get_personas_fallback")


def test_parse_object_and_clamp():
    assert parse_score('{"persona":"Cloud / DevOps Engineer","score":85,"reason":"Fits"}') \
        == (85, "Fits", "Cloud / DevOps Engineer")
    assert parse_score('{"score":150,"reason":"x"}')[0] == 100          # clamp
    assert parse_score('here: {"score": 40, "reason": "ok"} done')[0] == 40  # prose around
    assert parse_score('{"score":90,"reason":"y"}')[2] == ""            # persona optional
    assert parse_score('{"persona":"none","score":5,"reason":"z"}')[2] == ""  # 'none' -> empty
    print("PASS test_parse_object_and_clamp")


def test_parse_array_and_wrapped():
    assert parse_score('[{"score":80,"reason":"ok"}]')[:2] == (80, "ok")          # array form
    assert parse_score('{"result":{"score":70,"reason":"z"}}')[:2] == (70, "z")   # wrapped
    print("PASS test_parse_array_and_wrapped")


def test_parse_bad():
    assert parse_score("no json") is None
    assert parse_score('{"reason":"missing score"}') is None
    print("PASS test_parse_bad")


def test_backend_selection():
    saved = {k: os.environ.get(k) for k in ("LLM_BACKEND", "ANTHROPIC_API_KEY")}
    try:
        os.environ.pop("LLM_BACKEND", None)
        assert callable(make_caller())              # default ollama
        os.environ["LLM_BACKEND"] = "anthropic"
        os.environ.pop("ANTHROPIC_API_KEY", None)
        assert make_caller() is None                # anthropic without key
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    print("PASS test_backend_selection")


class _FakeHTTPResponse:
    def __init__(self, data: dict):
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


@contextmanager
def _fake_anthropic_post(response_data: dict, captured: list):
    """Swap out requests.post so anthropic_caller never touches the network."""
    original = scorer_mod.requests.post

    def _fake(*args, **kwargs):
        captured.append(kwargs)
        return _FakeHTTPResponse(response_data)

    scorer_mod.requests.post = _fake
    try:
        yield
    finally:
        scorer_mod.requests.post = original


def test_generator_backend_independent_of_scoring():
    # GENERATOR_BACKEND must not affect scoring's LLM_BACKEND, and vice versa.
    saved = {k: os.environ.get(k) for k in ("LLM_BACKEND", "GENERATOR_BACKEND", "ANTHROPIC_API_KEY")}
    try:
        os.environ.pop("LLM_BACKEND", None)
        os.environ["GENERATOR_BACKEND"] = "anthropic"
        os.environ["ANTHROPIC_API_KEY"] = "test-key"
        scoring_caller = make_caller(json_format=True)
        generator_caller = make_caller(json_format=False, backend_env="GENERATOR_BACKEND")
        assert "ollama_caller" in scoring_caller.__qualname__
        assert "anthropic_caller" in generator_caller.__qualname__
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    print("PASS test_generator_backend_independent_of_scoring")


def test_anthropic_extract_vs_write_model_and_tokens():
    # Scoring (json_format=True) gets the short/cheap EXTRACT pair; generation
    # (json_format=False) gets the long WRITE pair -- this is what let the old
    # hardcoded 512-token Anthropic letters get silently cut off.
    saved = {k: os.environ.get(k) for k in
             ("LLM_BACKEND", "GENERATOR_BACKEND", "ANTHROPIC_API_KEY",
              "ANTHROPIC_MODEL_EXTRACT", "ANTHROPIC_MODEL_WRITE")}
    try:
        os.environ["ANTHROPIC_API_KEY"] = "test-key"
        os.environ.pop("ANTHROPIC_MODEL_EXTRACT", None)
        os.environ.pop("ANTHROPIC_MODEL_WRITE", None)

        os.environ["LLM_BACKEND"] = "anthropic"
        scoring_caller = make_caller(json_format=True)
        captured = []
        with _fake_anthropic_post({"content": [{"type": "text", "text": "ok"}]}, captured):
            scoring_caller("sys", "user")
        assert captured[0]["json"]["model"] == ANTHROPIC_MODEL_EXTRACT
        assert captured[0]["json"]["max_tokens"] == 512

        os.environ["GENERATOR_BACKEND"] = "anthropic"
        gen_caller = make_caller(json_format=False, backend_env="GENERATOR_BACKEND")
        captured2 = []
        with _fake_anthropic_post({"content": [{"type": "text", "text": "ok"}]}, captured2):
            gen_caller("sys", "user")
        assert captured2[0]["json"]["model"] == ANTHROPIC_MODEL_WRITE
        assert captured2[0]["json"]["max_tokens"] == 2048
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    print("PASS test_anthropic_extract_vs_write_model_and_tokens")


def test_anthropic_caller_raises_on_truncated_reply():
    # A reply cut off by the token limit must be treated as a failure, never
    # returned to the caller as if it were a complete letter/summary.
    caller = scorer_mod.anthropic_caller("test-key", "claude-sonnet-5-5", max_tokens=2048)
    raised = False
    with _fake_anthropic_post({"stop_reason": "max_tokens",
                               "content": [{"type": "text", "text": "cut off mid"}]}, []):
        try:
            caller("sys", "user")
        except RuntimeError:
            raised = True
    assert raised
    print("PASS test_anthropic_caller_raises_on_truncated_reply")


def test_score_jobs_assigns_persona():
    def fake_caller(system, user):
        if "Bioinformatics Scientist" in user:
            return '{"persona": "Bioinformatics", "score": 90, "reason": "NGS work"}'
        return '{"persona": "Cloud / DevOps Engineer", "score": 82, "reason": "CI/CD work"}'
    n = score_jobs(JOBS, PROFILE, caller=fake_caller)
    assert n == 2
    assert JOBS[0].fit_score == 90 and JOBS[0].fit_persona == "Bioinformatics"
    assert JOBS[1].fit_score == 82 and JOBS[1].fit_persona == "Cloud / DevOps Engineer"
    print("PASS test_score_jobs_assigns_persona")


if __name__ == "__main__":
    test_personas_in_prompt()
    test_get_personas_fallback()
    test_parse_object_and_clamp()
    test_parse_array_and_wrapped()
    test_parse_bad()
    test_backend_selection()
    test_generator_backend_independent_of_scoring()
    test_anthropic_extract_vs_write_model_and_tokens()
    test_anthropic_caller_raises_on_truncated_reply()
    test_score_jobs_assigns_persona()
    print("\nAll scorer tests passed.")
