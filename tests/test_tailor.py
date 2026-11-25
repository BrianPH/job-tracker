"""Offline tests for jobtracker/tailor.py (issue #10): unit-tested against
mocked SDK responses -- no live Anthropic call. A fake client is injected via
the `client=` parameter (the module's own dependency-injection point), rather
than monkeypatching the `anthropic` package."""
import os
from contextlib import contextmanager

import yaml

from jobtracker.generation_schema import AnalysisResult, TailorResult
from jobtracker.tailor import (
    GenerationTruncatedError,
    GenerationUnavailableError,
    analyse_vacancy,
    make_client,
    tailor_application,
)

CANDIDATE = yaml.safe_load(open("config/candidate.example.yaml", encoding="utf-8"))


@contextmanager
def _no_api_key():
    """`client=None` only exercises the "no client available" path when
    ANTHROPIC_API_KEY is genuinely unset -- don't rely on the ambient
    environment (CI or a dev machine may have one set for other purposes)."""
    old = os.environ.pop("ANTHROPIC_API_KEY", None)
    try:
        yield
    finally:
        if old is not None:
            os.environ["ANTHROPIC_API_KEY"] = old


class _FakeUsage:
    def __init__(self, input_tokens=100, output_tokens=200):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class _FakeResponse:
    """Stands in for anthropic's ParsedMessage: stop_reason, parsed_output,
    usage, and a to_dict() for generation_records."""
    def __init__(self, parsed_output, stop_reason="end_turn", usage=None):
        self.parsed_output = parsed_output
        self.stop_reason = stop_reason
        self.usage = usage or _FakeUsage()
        self.model = "fake-model"

    def to_dict(self):
        return {"stop_reason": self.stop_reason, "model": self.model}


class _FakeMessages:
    def __init__(self, response, captured=None):
        self._response = response
        self._captured = captured if captured is not None else []

    def parse(self, **kwargs):
        self._captured.append(kwargs)
        return self._response


class _FakeClient:
    def __init__(self, response):
        self.captured = []
        self.messages = _FakeMessages(response, self.captured)


# --------------------------------------------------------------------------- #
# analyse_vacancy                                                             #
# --------------------------------------------------------------------------- #
def test_analyse_vacancy_valid_response():
    analysis = AnalysisResult(language="en", requirements=["Python", "SQL"],
                              keywords=["data", "pipelines"], contact="Jane Doe",
                              deadline="2026-12-01")
    client = _FakeClient(_FakeResponse(analysis))
    result = analyse_vacancy("Build pipelines with Python and SQL.", client=client)
    assert result is analysis
    assert client.captured[0]["output_format"] is AnalysisResult
    assert "Build pipelines" in client.captured[0]["messages"][0]["content"]
    print("PASS test_analyse_vacancy_valid_response")


def test_analyse_vacancy_truncated_raises():
    client = _FakeClient(_FakeResponse(AnalysisResult(language="en"), stop_reason="max_tokens"))
    try:
        analyse_vacancy("Some vacancy text.", client=client)
    except GenerationTruncatedError:
        pass
    else:
        raise AssertionError("expected GenerationTruncatedError")
    print("PASS test_analyse_vacancy_truncated_raises")


def test_analyse_vacancy_no_client_raises():
    with _no_api_key():
        try:
            analyse_vacancy("Some vacancy text.", client=None)
        except GenerationUnavailableError:
            pass
        else:
            raise AssertionError("expected GenerationUnavailableError")
    print("PASS test_analyse_vacancy_no_client_raises")


# --------------------------------------------------------------------------- #
# tailor_application                                                          #
# --------------------------------------------------------------------------- #
def _sample_tailor_result():
    return TailorResult(
        language="en", profile="A tailored profile. " * 10,
        experience_order=["examplecorp", "acmesoftware"],
        bullets={"examplecorp": ["examplecorp-pipelines"]},
        skills=["Python"],
        letter_paragraphs=["Paragraph one. " * 20, "Paragraph two. " * 20],
    )


def test_tailor_application_valid_response():
    result = _sample_tailor_result()
    client = _FakeClient(_FakeResponse(result))
    job = {"title": "Data Engineer", "company": "Acme", "description": "Build things."}
    analysis = AnalysisResult(language="en", requirements=["Python"])
    out = tailor_application(job, analysis, CANDIDATE, style_example="Dear team...", client=client)
    assert out is result
    kwargs = client.captured[0]
    assert kwargs["output_format"] is TailorResult
    assert "examplecorp" in kwargs["messages"][0]["content"]  # bullet bank reached the prompt
    assert "Data Engineer" in kwargs["messages"][0]["content"]
    print("PASS test_tailor_application_valid_response")


def test_tailor_application_truncated_raises():
    client = _FakeClient(_FakeResponse(_sample_tailor_result(), stop_reason="max_tokens"))
    job = {"title": "Data Engineer", "company": "Acme", "description": "Build things."}
    analysis = AnalysisResult(language="en")
    try:
        tailor_application(job, analysis, CANDIDATE, client=client)
    except GenerationTruncatedError:
        pass
    else:
        raise AssertionError("expected GenerationTruncatedError")
    print("PASS test_tailor_application_truncated_raises")


def test_tailor_application_no_client_raises():
    job = {"title": "Data Engineer", "company": "Acme", "description": "Build things."}
    analysis = AnalysisResult(language="en")
    with _no_api_key():
        try:
            tailor_application(job, analysis, CANDIDATE, client=None)
        except GenerationUnavailableError:
            pass
        else:
            raise AssertionError("expected GenerationUnavailableError")
    print("PASS test_tailor_application_no_client_raises")


def test_tailor_application_propagates_sdk_errors():
    # A malformed/rejected reply from the SDK (e.g. the server's JSON fails
    # Pydantic validation) must propagate, not be silently swallowed -- the
    # caller (not this module) decides whether to retry or fall back.
    class _RaisingMessages:
        def parse(self, **kwargs):
            raise ValueError("simulated schema validation failure")

    class _RaisingClient:
        messages = _RaisingMessages()

    job = {"title": "Data Engineer", "company": "Acme", "description": "Build things."}
    analysis = AnalysisResult(language="en")
    try:
        tailor_application(job, analysis, CANDIDATE, client=_RaisingClient())
    except ValueError as e:
        assert "simulated schema validation failure" in str(e)
    else:
        raise AssertionError("expected the SDK error to propagate")
    print("PASS test_tailor_application_propagates_sdk_errors")


def test_make_client_returns_none_without_api_key():
    with _no_api_key():
        assert make_client() is None
    print("PASS test_make_client_returns_none_without_api_key")


if __name__ == "__main__":
    test_analyse_vacancy_valid_response()
    test_analyse_vacancy_truncated_raises()
    test_analyse_vacancy_no_client_raises()
    test_tailor_application_valid_response()
    test_tailor_application_truncated_raises()
    test_tailor_application_no_client_raises()
    test_tailor_application_propagates_sdk_errors()
    test_make_client_returns_none_without_api_key()
    print("\nAll tailor tests passed.")
