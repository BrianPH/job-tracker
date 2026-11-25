"""Offline tests for jobtracker/generation.py (issue #11/M3b): the
analyse -> tailor orchestration and its JSON handoff fallback. Fake
Anthropic clients, same dependency-injection pattern as tests/test_tailor.py
-- no live API call."""
import json
import shutil
import tempfile
from pathlib import Path

import docx
import yaml

from jobtracker.generation import apply_handoff_upload, generate_or_handoff
from jobtracker.generation_schema import AnalysisResult, TailorResult
from jobtracker.tailor import CreditBalanceError

CANDIDATE = yaml.safe_load(open("config/candidate.example.yaml", encoding="utf-8"))
JOB = {"job_id": "test:job-1", "title": "Data Engineer", "company": "Acme", "location": "Remote",
      "description": "Build data pipelines with Python and SQL."}


class _FakeUsage:
    input_tokens = 100
    output_tokens = 200


class _FakeResponse:
    def __init__(self, parsed_output, stop_reason="end_turn"):
        self.parsed_output = parsed_output
        self.stop_reason = stop_reason
        self.usage = _FakeUsage()
        self.model = "fake-model"

    def to_dict(self):
        return {"stop_reason": self.stop_reason, "model": self.model}


class _QueuedMessages:
    """Returns the next (response_or_exception) from a queue per call --
    lets a test script "analyse succeeds, tailor fails" or similar
    multi-call sequences."""
    def __init__(self, queue):
        self._queue = list(queue)
        self.captured = []

    def parse(self, **kwargs):
        self.captured.append(kwargs)
        item = self._queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class _QueuedClient:
    def __init__(self, queue):
        self.messages = _QueuedMessages(queue)


def _good_tailor_result():
    return TailorResult(
        language="en", profile="A tailored profile. " * 10,
        experience_order=["examplecorp", "acmesoftware"],
        bullets={"examplecorp": ["examplecorp-pipelines"]},
        skills=["Python"],
        letter_paragraphs=["Paragraph one. " * 20, "Paragraph two. " * 20],
    )


def _with_tmp_out(fn):
    out = Path(tempfile.mkdtemp(prefix="jt_handoff_"))
    try:
        return fn(out)
    finally:
        shutil.rmtree(out, ignore_errors=True)


# --------------------------------------------------------------------------- #
# generate_or_handoff: success path                                          #
# --------------------------------------------------------------------------- #
def test_generate_or_handoff_renders_on_success():
    def run(out):
        analysis = AnalysisResult(language="en", requirements=["Python"])
        client = _QueuedClient([_FakeResponse(analysis), _FakeResponse(_good_tailor_result())])
        statuses = []
        result = generate_or_handoff(JOB, candidate=CANDIDATE, out_dir=out, client=client,
                                     set_status=statuses.append)
        assert result["state"] == "done"
        assert result["cv"].exists() and result["letter"].exists()
        assert statuses == []  # status untouched on success
        letter = "\n".join(p.text for p in docx.Document(str(result["letter"])).paragraphs)
        assert "Paragraph one." in letter
        print("PASS test_generate_or_handoff_renders_on_success")
    _with_tmp_out(run)


# --------------------------------------------------------------------------- #
# generate_or_handoff: one error class per handoff trigger                   #
# --------------------------------------------------------------------------- #
def test_generate_or_handoff_no_client_triggers_handoff():
    import os
    def run(out):
        old_key = os.environ.pop("ANTHROPIC_API_KEY", None)
        old_token = os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)
        try:
            statuses = []
            result = generate_or_handoff(JOB, candidate=CANDIDATE, out_dir=out, client=None,
                                         set_status=statuses.append)
        finally:
            if old_key is not None:
                os.environ["ANTHROPIC_API_KEY"] = old_key
            if old_token is not None:
                os.environ["ANTHROPIC_AUTH_TOKEN"] = old_token
        assert result["state"] == "handoff"
        assert statuses == ["Wacht op generatie"]
        payload = json.loads(result["request_path"].read_text(encoding="utf-8"))
        assert payload["analysis"] is None
        assert payload["job"]["title"] == "Data Engineer"
        assert payload["job"]["job_id"] == "test:job-1"
        print("PASS test_generate_or_handoff_no_client_triggers_handoff")
    _with_tmp_out(run)


def test_generate_or_handoff_credit_balance_triggers_handoff():
    def run(out):
        client = _QueuedClient([CreditBalanceError("no credit")])
        statuses = []
        result = generate_or_handoff(JOB, candidate=CANDIDATE, out_dir=out, client=client,
                                     set_status=statuses.append)
        assert result["state"] == "handoff"
        assert statuses == ["Wacht op generatie"]
        print("PASS test_generate_or_handoff_credit_balance_triggers_handoff")
    _with_tmp_out(run)


def test_generate_or_handoff_truncated_tailor_reply_triggers_handoff():
    def run(out):
        analysis = AnalysisResult(language="en", requirements=["Python"])
        client = _QueuedClient([
            _FakeResponse(analysis),
            _FakeResponse(_good_tailor_result(), stop_reason="max_tokens"),
        ])
        result = generate_or_handoff(JOB, candidate=CANDIDATE, out_dir=out, client=client)
        assert result["state"] == "handoff"
        payload = json.loads(result["request_path"].read_text(encoding="utf-8"))
        assert payload["analysis"]["language"] == "en"  # analyse succeeded before tailor failed
        print("PASS test_generate_or_handoff_truncated_tailor_reply_triggers_handoff")
    _with_tmp_out(run)


def test_generate_or_handoff_persistent_validation_errors_trigger_handoff():
    def run(out):
        analysis = AnalysisResult(language="en", requirements=["Python"])
        bad = TailorResult(language="en", profile="A tailored profile. " * 10,
                           experience_order=["examplecorp"],
                           bullets={"examplecorp": ["no-such-bullet"]},
                           letter_paragraphs=["Paragraph one. " * 20, "Paragraph two. " * 20])
        # analyse, then tailor (invalid), then the one retry (still invalid)
        client = _QueuedClient([_FakeResponse(analysis), _FakeResponse(bad), _FakeResponse(bad)])
        result = generate_or_handoff(JOB, candidate=CANDIDATE, out_dir=out, client=client)
        assert result["state"] == "handoff"
        assert "no-such-bullet" in result["error"]
        print("PASS test_generate_or_handoff_persistent_validation_errors_trigger_handoff")
    _with_tmp_out(run)


# --------------------------------------------------------------------------- #
# apply_handoff_upload                                                       #
# --------------------------------------------------------------------------- #
def test_apply_handoff_upload_valid_renders_and_clears_status():
    def run(out):
        statuses = []
        generation_json = _good_tailor_result().model_dump()
        result = apply_handoff_upload(JOB, generation_json, candidate=CANDIDATE, out_dir=out,
                                      set_status=statuses.append)
        assert result["state"] == "done"
        assert result["cv"].exists() and result["letter"].exists()
        assert statuses == ["New"]
        print("PASS test_apply_handoff_upload_valid_renders_and_clears_status")
    _with_tmp_out(run)


def test_apply_handoff_upload_invalid_shape_renders_nothing():
    def run(out):
        statuses = []
        # missing required fields (language, profile) -- fails TailorResult itself
        result = apply_handoff_upload(JOB, {"letter_paragraphs": ["x"]}, candidate=CANDIDATE,
                                      out_dir=out, set_status=statuses.append)
        assert result["state"] == "invalid"
        assert statuses == []
        assert not any(out.iterdir())
        print("PASS test_apply_handoff_upload_invalid_shape_renders_nothing")
    _with_tmp_out(run)


def test_apply_handoff_upload_invalid_ids_renders_nothing():
    def run(out):
        # structurally valid TailorResult JSON, but references a bullet id
        # that doesn't exist in CANDIDATE -- same validator as the live path.
        bad = TailorResult(language="en", profile="A tailored profile. " * 10,
                           experience_order=["examplecorp"],
                           bullets={"examplecorp": ["no-such-bullet"]},
                           letter_paragraphs=["Paragraph one. " * 20, "Paragraph two. " * 20])
        result = apply_handoff_upload(JOB, bad.model_dump(), candidate=CANDIDATE, out_dir=out)
        assert result["state"] == "invalid"
        assert any("no-such-bullet" in e for e in result["errors"])
        print("PASS test_apply_handoff_upload_invalid_ids_renders_nothing")
    _with_tmp_out(run)


if __name__ == "__main__":
    test_generate_or_handoff_renders_on_success()
    test_generate_or_handoff_no_client_triggers_handoff()
    test_generate_or_handoff_credit_balance_triggers_handoff()
    test_generate_or_handoff_truncated_tailor_reply_triggers_handoff()
    test_generate_or_handoff_persistent_validation_errors_trigger_handoff()
    test_apply_handoff_upload_valid_renders_and_clears_status()
    test_apply_handoff_upload_invalid_shape_renders_nothing()
    test_apply_handoff_upload_invalid_ids_renders_nothing()
    print("\nAll generation tests passed.")
