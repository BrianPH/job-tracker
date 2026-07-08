"""Offline tests for the LLM scorer: prompt building, per-job response parsing,
backend selection, and assignment. Fake caller — no network, no server.
Run:  python -m tests.test_scorer
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jobtracker.models import Job
from jobtracker.scorer import build_job_prompt, parse_score, score_jobs, make_caller

PROFILE = {
    "profile": {"summary": "Bioinformatician, Python, NGS pipelines.",
                "must_haves": ["Python"], "nice_to_haves": ["cloud"]},
    "rules": {"keywords_any": ["bioinformatics", "genomics"]},
}
JOBS = [
    Job(title="Bioinformatics Scientist", company="GenomeScan", source="Recruitee",
        description="Build NGS pipelines in Python.", job_id="a1"),
    Job(title="Warehouse Operator", company="Amgen", source="Workday",
        description="Move boxes.", job_id="b2"),
]


def test_build_job_prompt():
    system, user = build_job_prompt(PROFILE, JOBS[0])
    assert "JSON object" in system
    assert "Bioinformatics Scientist" in user and "GenomeScan" in user
    assert "Bioinformatician" in user  # profile included
    print("PASS test_build_job_prompt")


def test_parse_object_and_clamp():
    assert parse_score('{"score":92,"reason":"Great fit"}') == (92, "Great fit")
    assert parse_score('{"score":150,"reason":"x"}')[0] == 100      # clamp
    assert parse_score('here: {"score": 40, "reason": "ok"} done')[0] == 40  # prose around
    print("PASS test_parse_object_and_clamp")


def test_parse_array_and_wrapped():
    assert parse_score('[{"score":80,"reason":"ok"}]') == (80, "ok")     # array form
    assert parse_score('{"result":{"score":70,"reason":"z"}}') == (70, "z")  # wrapped
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
            os.environ[k] = v if v is not None else os.environ.pop(k, "") and ""
            if v is None:
                os.environ.pop(k, None)
    print("PASS test_backend_selection")


def test_score_jobs_assigns():
    def fake_caller(system, user):
        score = 90 if "Bioinformatics" in user else 5
        return f'{{"score": {score}, "reason": "auto"}}'
    n = score_jobs(JOBS, PROFILE, caller=fake_caller)
    assert n == 2
    assert JOBS[0].fit_score == 90 and JOBS[1].fit_score == 5
    print("PASS test_score_jobs_assigns")


if __name__ == "__main__":
    test_build_job_prompt()
    test_parse_object_and_clamp()
    test_parse_array_and_wrapped()
    test_parse_bad()
    test_backend_selection()
    test_score_jobs_assigns()
    print("\nAll scorer tests passed.")
