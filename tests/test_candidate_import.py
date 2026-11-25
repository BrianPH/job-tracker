"""Offline tests for the candidate.yaml import helper (issue #9): a fixed fake
LLM reply, never a live call, matching the acceptance criteria
("tested against a fixture, not a live LLM call")."""
import json
import shutil
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import import_candidate  # noqa: E402  -- root-level CLI script
from jobtracker.candidate_import import (  # noqa: E402
    CandidateImportError,
    draft_candidate_from_text,
    write_candidate_draft,
)

FIXTURE_CV_TEXT = (
    "Alex Example\n"
    "alex.example@example.com | +31 6 00000000\n\n"
    "EXPERIENCE\n"
    "Software Developer, Example Corp, 2023 - present\n"
    "- Built and maintained Python data pipelines processing thousands of records daily.\n\n"
    "EDUCATION\n"
    "BSc Computer Science, Example University, 2018 - 2022\n"
)

FIXTURE_REPLY = json.dumps({
    "personal": {"name": "Alex Example", "address": "Utrecht, NL",
                "email": "alex.example@example.com", "phone": "+31 6 00000000"},
    "experience": [{
        "id": "examplecorp", "title": "Software Ontwikkelaar", "title_en": "Software Developer",
        "company": "Example Corp", "period": "2023 - heden", "period_en": "2023 - present",
        "bullets": [{
            "id": "examplecorp-pipelines", "tags": ["python", "data"],
            "nl": "Bouwde en onderhield Python-datapipelines.",
            "en": "Built and maintained Python data pipelines.",
        }],
    }],
    "education": [{
        "id": "example-university", "degree": "BSc Informatica", "degree_en": "BSc Computer Science",
        "school": "Example University", "school_en": "Example University",
        "period": "2018 - 2022", "period_en": "2018 - 2022",
    }],
    "skills": [{"name": "Python", "level": "Uitgebreid", "level_en": "Extensive", "tags": ["python"]}],
    "languages": [{"name": "Engels", "name_en": "English", "level": "Vloeiend", "level_en": "Fluent"}],
    "highlights": [],
})


def _fixed_caller(system, user):
    return FIXTURE_REPLY


def test_draft_candidate_from_text_parses_valid_reply():
    data = draft_candidate_from_text(FIXTURE_CV_TEXT, _fixed_caller)
    assert data["personal"]["name"] == "Alex Example"
    assert data["experience"][0]["id"] == "examplecorp"
    assert data["experience"][0]["bullets"][0]["id"] == "examplecorp-pipelines"
    print("PASS test_draft_candidate_from_text_parses_valid_reply")


def test_draft_candidate_from_text_tolerates_markdown_fence():
    fenced = f"```json\n{FIXTURE_REPLY}\n```"
    data = draft_candidate_from_text(FIXTURE_CV_TEXT, lambda s, u: fenced)
    assert data["personal"]["name"] == "Alex Example"
    print("PASS test_draft_candidate_from_text_tolerates_markdown_fence")


def test_draft_candidate_from_text_rejects_invalid_json():
    try:
        draft_candidate_from_text(FIXTURE_CV_TEXT, lambda s, u: "not json at all")
    except CandidateImportError:
        pass
    else:
        raise AssertionError("expected CandidateImportError")
    print("PASS test_draft_candidate_from_text_rejects_invalid_json")


def test_draft_candidate_from_text_rejects_missing_top_level_key():
    bad = json.loads(FIXTURE_REPLY)
    del bad["education"]

    try:
        draft_candidate_from_text(FIXTURE_CV_TEXT, lambda s, u: json.dumps(bad))
    except CandidateImportError as e:
        assert "education" in str(e)
    else:
        raise AssertionError("expected CandidateImportError")
    print("PASS test_draft_candidate_from_text_rejects_missing_top_level_key")


def test_draft_candidate_from_text_rejects_experience_without_id():
    bad = json.loads(FIXTURE_REPLY)
    del bad["experience"][0]["id"]

    try:
        draft_candidate_from_text(FIXTURE_CV_TEXT, lambda s, u: json.dumps(bad))
    except CandidateImportError as e:
        assert "experience[0]" in str(e)
    else:
        raise AssertionError("expected CandidateImportError")
    print("PASS test_draft_candidate_from_text_rejects_experience_without_id")


def test_draft_candidate_from_text_rejects_bullet_without_id():
    bad = json.loads(FIXTURE_REPLY)
    del bad["experience"][0]["bullets"][0]["id"]

    try:
        draft_candidate_from_text(FIXTURE_CV_TEXT, lambda s, u: json.dumps(bad))
    except CandidateImportError as e:
        assert "bullets[0]" in str(e)
    else:
        raise AssertionError("expected CandidateImportError")
    print("PASS test_draft_candidate_from_text_rejects_bullet_without_id")


def test_write_candidate_draft_roundtrips_as_yaml():
    tmp = Path(tempfile.mkdtemp(prefix="jt_import_"))
    try:
        data = draft_candidate_from_text(FIXTURE_CV_TEXT, _fixed_caller)
        out_path = write_candidate_draft(data, tmp / "candidate.draft.yaml")
        assert out_path.exists()
        reloaded = yaml.safe_load(out_path.read_text(encoding="utf-8"))
        assert reloaded == data
        print("PASS test_write_candidate_draft_roundtrips_as_yaml")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------- #
# CLI entry point (import_candidate.py), LLM call mocked -- no live network   #
# --------------------------------------------------------------------------- #
def test_cli_writes_draft_with_mocked_caller():
    tmp = Path(tempfile.mkdtemp(prefix="jt_import_cli_"))
    try:
        cv_file = tmp / "cv.txt"
        cv_file.write_text(FIXTURE_CV_TEXT, encoding="utf-8")
        out_file = tmp / "candidate.draft.yaml"

        with patch.object(import_candidate, "make_caller", lambda **kwargs: _fixed_caller), \
             patch.object(import_candidate, "load_env", lambda: None):
            code = import_candidate.main([str(cv_file), "--out", str(out_file)])
        assert code == 0
        assert out_file.exists()
        data = yaml.safe_load(out_file.read_text(encoding="utf-8"))
        assert data["personal"]["name"] == "Alex Example"
        print("PASS test_cli_writes_draft_with_mocked_caller")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_cli_exits_nonzero_when_no_backend_available():
    tmp = Path(tempfile.mkdtemp(prefix="jt_import_cli_"))
    try:
        cv_file = tmp / "cv.txt"
        cv_file.write_text(FIXTURE_CV_TEXT, encoding="utf-8")

        with patch.object(import_candidate, "make_caller", lambda **kwargs: None), \
             patch.object(import_candidate, "load_env", lambda: None):
            code = import_candidate.main([str(cv_file)])
        assert code == 1
        print("PASS test_cli_exits_nonzero_when_no_backend_available")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_cli_exits_nonzero_for_missing_input_file():
    code = import_candidate.main(["/no/such/file.txt"])
    assert code == 2
    print("PASS test_cli_exits_nonzero_for_missing_input_file")


if __name__ == "__main__":
    test_draft_candidate_from_text_parses_valid_reply()
    test_draft_candidate_from_text_tolerates_markdown_fence()
    test_draft_candidate_from_text_rejects_invalid_json()
    test_draft_candidate_from_text_rejects_missing_top_level_key()
    test_draft_candidate_from_text_rejects_experience_without_id()
    test_draft_candidate_from_text_rejects_bullet_without_id()
    test_write_candidate_draft_roundtrips_as_yaml()
    test_cli_writes_draft_with_mocked_caller()
    test_cli_exits_nonzero_when_no_backend_available()
    test_cli_exits_nonzero_for_missing_input_file()
    print("\nAll candidate_import tests passed.")
