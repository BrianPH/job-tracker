"""Offline tests for jobtracker/tailor.py (issue #10): unit-tested against
mocked SDK responses -- no live Anthropic call. A fake client is injected via
the `client=` parameter (the module's own dependency-injection point), rather
than monkeypatching the `anthropic` package."""
import os
from contextlib import contextmanager

import yaml
from pydantic import ValidationError

from jobtracker.generation_schema import AnalysisResult, TailorResult
from jobtracker.tailor import (
    VACANCY_DESC_CHARS,
    CreditBalanceError,
    GenerationTruncatedError,
    GenerationUnavailableError,
    allowed_terms,
    analyse_vacancy,
    make_client,
    run_tailor_pipeline,
    tailor_application,
)

CANDIDATE = yaml.safe_load(open("config/candidate.example.yaml", encoding="utf-8"))


@contextmanager
def _no_credentials():
    """`client=None` only exercises the "no client available" path when
    neither credential env var is genuinely set -- don't rely on the ambient
    environment (CI or a dev machine may have one set for other purposes)."""
    old_key = os.environ.pop("ANTHROPIC_API_KEY", None)
    old_token = os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)
    try:
        yield
    finally:
        if old_key is not None:
            os.environ["ANTHROPIC_API_KEY"] = old_key
        if old_token is not None:
            os.environ["ANTHROPIC_AUTH_TOKEN"] = old_token


# Kept as an alias: most tests only care about ANTHROPIC_API_KEY being unset.
_no_api_key = _no_credentials


def _real_validation_error() -> ValidationError:
    """A genuine pydantic.ValidationError, the same exception type
    `messages.parse()` raises internally on a truncated/malformed reply --
    constructed by actually failing validation, not hand-built, so the test
    exercises the real exception type and not a stand-in."""
    try:
        AnalysisResult()  # missing required field `language`
    except ValidationError as e:
        return e
    raise AssertionError("expected AnalysisResult() to fail validation")


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


class _SequentialMessages:
    """Returns one response per call, in order -- for the run_tailor_pipeline
    retry test, where the first and second calls must return different
    TailorResults (first invalid, then fixed)."""
    def __init__(self, responses, captured):
        self._responses = list(responses)
        self._captured = captured

    def parse(self, **kwargs):
        self._captured.append(kwargs)
        return self._responses.pop(0)


class _SequentialClient:
    def __init__(self, responses):
        self.captured = []
        self.messages = _SequentialMessages(responses, self.captured)


def _fake_status_error(error_cls, status_code: int, message: str, error_type: str):
    """Builds a genuine anthropic.APIStatusError subclass the way the SDK
    itself does -- not a stand-in -- so an `except <SDK type>` clause
    actually matches it."""
    import httpx2
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    body = {"type": "error", "error": {"type": error_type, "message": message}}
    response = httpx2.Response(status_code, request=request, json=body)
    return error_cls(message, response=response, body=body)


def _fake_payment_required_error() -> "Exception":
    """A genuine anthropic.PaymentRequiredError (402) -- see
    _fake_status_error -- so _parse_or_raise's
    `except anthropic.PaymentRequiredError` actually matches it."""
    import anthropic
    return _fake_status_error(anthropic.PaymentRequiredError, 402,
                              "Your credit balance is too low.", "payment_required_error")


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
    # issue #12's manual live-API check found Sonnet 5's default adaptive
    # thinking truncating a real tailor reply well under TAILOR_MAX_TOKENS --
    # thinking must stay explicitly disabled on both calls.
    assert client.captured[0]["thinking"] == {"type": "disabled"}
    print("PASS test_analyse_vacancy_valid_response")


def test_analyse_vacancy_truncated_raises():
    # Defensive path: a reply that DOES parse but is still marked
    # stop_reason="max_tokens" (e.g. cut exactly on a JSON boundary).
    client = _FakeClient(_FakeResponse(AnalysisResult(language="en"), stop_reason="max_tokens"))
    try:
        analyse_vacancy("Some vacancy text.", client=client)
    except GenerationTruncatedError:
        pass
    else:
        raise AssertionError("expected GenerationTruncatedError")
    print("PASS test_analyse_vacancy_truncated_raises")


def test_analyse_vacancy_genuinely_truncated_raises():
    # Realistic path: a genuinely truncated reply is incomplete JSON, so the
    # SDK's own `messages.parse()` fails Pydantic validation internally and
    # never returns a response object with a readable `.stop_reason` at all.
    class _TruncatingMessages:
        def parse(self, **kwargs):
            raise _real_validation_error()

    class _TruncatingClient:
        messages = _TruncatingMessages()

    try:
        analyse_vacancy("Some vacancy text.", client=_TruncatingClient())
    except GenerationTruncatedError:
        pass
    else:
        raise AssertionError("expected GenerationTruncatedError")
    print("PASS test_analyse_vacancy_genuinely_truncated_raises")


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
def _prompt_text(kwargs: dict) -> str:
    """issue #13: tailor_application's user content is now a list of two
    text blocks (facts block + vacancy block, so the facts block can carry
    `cache_control`) instead of one plain string. Tests that only care about
    "does the prompt contain X" don't need to care about that split -- join
    both blocks' text back into one string, same as before the change."""
    content = kwargs["messages"][0]["content"]
    if isinstance(content, str):
        return content
    return "\n".join(block["text"] for block in content)


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
    prompt = _prompt_text(kwargs)
    assert "examplecorp" in prompt  # bullet bank reached the prompt
    assert "Data Engineer" in prompt
    assert "example-university" in prompt  # education ids too
    assert kwargs["thinking"] == {"type": "disabled"}
    print("PASS test_tailor_application_valid_response")


def test_analyse_vacancy_caps_vacancy_text_at_desc_chars():
    long_text = ("Relevant requirement near the end. " * 200)
    assert len(long_text) > VACANCY_DESC_CHARS
    client = _FakeClient(_FakeResponse(AnalysisResult(language="en")))
    analyse_vacancy(long_text, client=client)
    prompt = client.captured[0]["messages"][0]["content"]
    assert long_text not in prompt
    assert long_text[:VACANCY_DESC_CHARS] in prompt
    print("PASS test_analyse_vacancy_caps_vacancy_text_at_desc_chars")


def test_tailor_application_caps_description_at_desc_chars():
    long_description = "x" * (VACANCY_DESC_CHARS * 2)
    client = _FakeClient(_FakeResponse(_sample_tailor_result()))
    job = {"title": "Data Engineer", "company": "Acme", "description": long_description}
    analysis = AnalysisResult(language="en")
    tailor_application(job, analysis, CANDIDATE, client=client)
    prompt = _prompt_text(client.captured[0])
    assert long_description not in prompt
    assert ("x" * VACANCY_DESC_CHARS) in prompt
    assert ("x" * (VACANCY_DESC_CHARS + 1)) not in prompt
    print("PASS test_tailor_application_caps_description_at_desc_chars")


def test_allowed_terms_includes_company_and_keywords():
    job = {"title": "Data Engineer", "company": "Initech", "description": "Build things."}
    analysis = AnalysisResult(language="en", keywords=["Kubernetes", "AWS"])
    terms = allowed_terms(job, analysis)
    assert "Initech" in terms
    assert "Kubernetes" in terms and "AWS" in terms
    print("PASS test_allowed_terms_includes_company_and_keywords")


def test_allowed_terms_splits_multi_word_keywords():
    # ground_tailor_result checks one capitalized token at a time -- a
    # multi-word keyword like "Azure DevOps" must also allow "Azure" and
    # "DevOps" individually, or each word of a keyword the letter
    # legitimately echoes back flags as an unverified fact.
    job = {"title": "Data Engineer", "company": "Acme", "description": "Build things."}
    analysis = AnalysisResult(language="en", keywords=["Azure DevOps"])
    terms = allowed_terms(job, analysis)
    assert "Azure DevOps" in terms
    assert "Azure" in terms and "DevOps" in terms
    print("PASS test_allowed_terms_splits_multi_word_keywords")


def test_allowed_terms_skips_missing_company():
    job = {"title": "Data Engineer", "description": "Build things."}
    analysis = AnalysisResult(language="en")
    assert allowed_terms(job, analysis) == []
    print("PASS test_allowed_terms_skips_missing_company")


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


def test_tailor_application_genuinely_truncated_raises():
    class _TruncatingMessages:
        def parse(self, **kwargs):
            raise _real_validation_error()

    class _TruncatingClient:
        messages = _TruncatingMessages()

    job = {"title": "Data Engineer", "company": "Acme", "description": "Build things."}
    analysis = AnalysisResult(language="en")
    try:
        tailor_application(job, analysis, CANDIDATE, client=_TruncatingClient())
    except GenerationTruncatedError:
        pass
    else:
        raise AssertionError("expected GenerationTruncatedError")
    print("PASS test_tailor_application_genuinely_truncated_raises")


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


# --------------------------------------------------------------------------- #
# issue #11/M3b: credit-balance classification + validator-error retry        #
# --------------------------------------------------------------------------- #
def test_tailor_application_includes_retry_errors_in_prompt():
    result = _sample_tailor_result()
    client = _FakeClient(_FakeResponse(result))
    job = {"title": "Data Engineer", "company": "Acme", "description": "Build things."}
    analysis = AnalysisResult(language="en")
    tailor_application(job, analysis, CANDIDATE, client=client,
                       retry_errors=["bullets['x'] references unknown bullet id 'y'."])
    prompt = _prompt_text(client.captured[0])
    assert "failed validation" in prompt
    assert "unknown bullet id 'y'" in prompt
    print("PASS test_tailor_application_includes_retry_errors_in_prompt")


def test_parse_or_raise_translates_payment_required_to_credit_balance_error():
    class _PaymentFailingMessages:
        def parse(self, **kwargs):
            raise _fake_payment_required_error()

    class _PaymentFailingClient:
        messages = _PaymentFailingMessages()

    job = {"title": "Data Engineer", "company": "Acme", "description": "Build things."}
    analysis = AnalysisResult(language="en")
    try:
        tailor_application(job, analysis, CANDIDATE, client=_PaymentFailingClient())
    except CreditBalanceError:
        pass
    else:
        raise AssertionError("expected CreditBalanceError")
    print("PASS test_parse_or_raise_translates_payment_required_to_credit_balance_error")


def test_parse_or_raise_does_not_misclassify_an_ordinary_bad_request_error():
    # Acceptance criterion: a 400 that isn't a credit-balance error must NOT
    # be treated as one -- it should propagate as the SDK's own
    # BadRequestError, not CreditBalanceError.
    import anthropic

    class _BadRequestMessages:
        def parse(self, **kwargs):
            raise _fake_status_error(anthropic.BadRequestError, 400,
                                     "max_tokens must be at least 1.", "invalid_request_error")

    class _BadRequestClient:
        messages = _BadRequestMessages()

    job = {"title": "Data Engineer", "company": "Acme", "description": "Build things."}
    analysis = AnalysisResult(language="en")
    try:
        tailor_application(job, analysis, CANDIDATE, client=_BadRequestClient())
    except CreditBalanceError:
        raise AssertionError("an ordinary 400 must not be classified as CreditBalanceError")
    except anthropic.BadRequestError:
        pass
    print("PASS test_parse_or_raise_does_not_misclassify_an_ordinary_bad_request_error")


def test_run_tailor_pipeline_no_retry_when_first_reply_is_valid():
    result = _sample_tailor_result()
    client = _SequentialClient([_FakeResponse(result)])
    job = {"title": "Data Engineer", "company": "Acme", "description": "Build things."}
    analysis = AnalysisResult(language="en")
    out, errors = run_tailor_pipeline(job, analysis, CANDIDATE, client=client)
    assert out is result
    assert errors == []
    assert len(client.captured) == 1  # no retry needed
    print("PASS test_run_tailor_pipeline_no_retry_when_first_reply_is_valid")


def test_run_tailor_pipeline_retries_once_then_succeeds():
    bad = TailorResult(language="en", profile="A tailored profile. " * 10,
                       experience_order=["examplecorp"],
                       bullets={"examplecorp": ["no-such-bullet"]},
                       letter_paragraphs=["Paragraph one. " * 20, "Paragraph two. " * 20])
    good = _sample_tailor_result()
    client = _SequentialClient([_FakeResponse(bad), _FakeResponse(good)])
    job = {"title": "Data Engineer", "company": "Acme", "description": "Build things."}
    analysis = AnalysisResult(language="en")
    out, errors = run_tailor_pipeline(job, analysis, CANDIDATE, client=client)
    assert out is good
    assert errors == []
    assert len(client.captured) == 2
    assert "no-such-bullet" in _prompt_text(client.captured[1])
    print("PASS test_run_tailor_pipeline_retries_once_then_succeeds")


def test_run_tailor_pipeline_returns_remaining_errors_when_retry_also_fails():
    bad = TailorResult(language="en", profile="A tailored profile. " * 10,
                       experience_order=["examplecorp"],
                       bullets={"examplecorp": ["no-such-bullet"]},
                       letter_paragraphs=["Paragraph one. " * 20, "Paragraph two. " * 20])
    still_bad = TailorResult(language="en", profile="A tailored profile. " * 10,
                             experience_order=["examplecorp"],
                             bullets={"examplecorp": ["still-no-such-bullet"]},
                             letter_paragraphs=["Paragraph one. " * 20, "Paragraph two. " * 20])
    client = _SequentialClient([_FakeResponse(bad), _FakeResponse(still_bad)])
    job = {"title": "Data Engineer", "company": "Acme", "description": "Build things."}
    analysis = AnalysisResult(language="en")
    out, errors = run_tailor_pipeline(job, analysis, CANDIDATE, client=client)
    assert out is still_bad
    assert len(errors) == 1 and "still-no-such-bullet" in errors[0]
    assert len(client.captured) == 2  # only one retry, not a retry loop
    print("PASS test_run_tailor_pipeline_returns_remaining_errors_when_retry_also_fails")


# --------------------------------------------------------------------------- #
# issue #13: prompt caching + generation_records wiring                       #
# --------------------------------------------------------------------------- #
class _FakeRecordStore:
    def __init__(self):
        self.calls = []

    def record(self, **kwargs):
        self.calls.append(kwargs)
        return {"record_id": "fake-id"}


def test_tailor_application_caches_only_the_facts_block():
    # The facts block (bullet bank/education/skills/style example) is
    # identical across every vacancy for a given candidate -- it must carry
    # cache_control and come FIRST (caching needs the stable prefix first).
    # The vacancy/analysis block changes every call, so it must NOT be
    # cached.
    client = _FakeClient(_FakeResponse(_sample_tailor_result()))
    job = {"title": "Data Engineer", "company": "Acme", "description": "Build things."}
    analysis = AnalysisResult(language="en", requirements=["Python"])
    tailor_application(job, analysis, CANDIDATE, style_example="Dear team...", client=client)
    content = client.captured[0]["messages"][0]["content"]
    assert len(content) == 2
    facts_block, vacancy_block = content
    assert facts_block.get("cache_control") == {"type": "ephemeral"}
    assert "BULLET BANK" in facts_block["text"]
    assert "cache_control" not in vacancy_block
    assert "Data Engineer" in vacancy_block["text"]
    print("PASS test_tailor_application_caches_only_the_facts_block")


def test_tailor_application_facts_block_is_stable_across_calls():
    # Caching only helps if the cached block is byte-identical call to call
    # for the same candidate/language -- changing the vacancy must not touch
    # the facts block's text at all.
    client = _FakeClient(_FakeResponse(_sample_tailor_result()))
    analysis = AnalysisResult(language="en")
    tailor_application({"title": "Data Engineer", "company": "Acme", "description": "A"},
                       analysis, CANDIDATE, style_example="Dear team...", client=client)
    tailor_application({"title": "Backend Engineer", "company": "Other", "description": "B"},
                       analysis, CANDIDATE, style_example="Dear team...", client=client)
    first_facts = client.captured[0]["messages"][0]["content"][0]["text"]
    second_facts = client.captured[1]["messages"][0]["content"][0]["text"]
    assert first_facts == second_facts
    print("PASS test_tailor_application_facts_block_is_stable_across_calls")


def test_analyse_vacancy_records_only_when_job_id_and_store_given():
    store = _FakeRecordStore()
    client = _FakeClient(_FakeResponse(AnalysisResult(language="en")))
    analyse_vacancy("Some vacancy text.", client=client)  # no job_id/store
    assert store.calls == []

    analyse_vacancy("Some vacancy text.", client=client, job_id="job-1", record_store=store)
    assert len(store.calls) == 1
    assert store.calls[0]["job_id"] == "job-1"
    assert store.calls[0]["stage"] == "analyse"
    print("PASS test_analyse_vacancy_records_only_when_job_id_and_store_given")


def test_tailor_application_records_cache_tokens():
    class _CacheUsage(_FakeUsage):
        def __init__(self):
            super().__init__()
            self.cache_creation_input_tokens = 500
            self.cache_read_input_tokens = 50

    store = _FakeRecordStore()
    client = _FakeClient(_FakeResponse(_sample_tailor_result(), usage=_CacheUsage()))
    job = {"title": "Data Engineer", "company": "Acme", "description": "Build things.",
          "job_id": "job-2"}
    analysis = AnalysisResult(language="en")
    tailor_application(job, analysis, CANDIDATE, client=client,
                       job_id="job-2", record_store=store)
    assert len(store.calls) == 1
    assert store.calls[0]["cache_creation_input_tokens"] == 500
    assert store.calls[0]["cache_read_input_tokens"] == 50
    assert store.calls[0]["stage"] == "tailor"
    print("PASS test_tailor_application_records_cache_tokens")


def test_record_store_failure_does_not_break_generation():
    # The generation already succeeded by the time recording runs -- a
    # record_store write failure (disk full, DB down) must not turn a
    # working generation into an error.
    class _BrokenStore:
        def record(self, **kwargs):
            raise RuntimeError("disk full")

    result = _sample_tailor_result()
    client = _FakeClient(_FakeResponse(result))
    job = {"title": "Data Engineer", "company": "Acme", "description": "Build things."}
    analysis = AnalysisResult(language="en")
    out = tailor_application(job, analysis, CANDIDATE, client=client,
                             job_id="job-3", record_store=_BrokenStore())
    assert out is result
    print("PASS test_record_store_failure_does_not_break_generation")


def test_run_tailor_pipeline_records_each_call():
    bad = TailorResult(language="en", profile="A tailored profile. " * 10,
                       experience_order=["examplecorp"],
                       bullets={"examplecorp": ["no-such-bullet"]},
                       letter_paragraphs=["Paragraph one. " * 20, "Paragraph two. " * 20])
    good = _sample_tailor_result()
    store = _FakeRecordStore()
    client = _SequentialClient([_FakeResponse(bad), _FakeResponse(good)])
    job = {"title": "Data Engineer", "company": "Acme", "description": "Build things."}
    analysis = AnalysisResult(language="en")
    run_tailor_pipeline(job, analysis, CANDIDATE, client=client,
                        job_id="job-4", record_store=store)
    assert len(store.calls) == 2  # initial attempt + retry, one row each
    print("PASS test_run_tailor_pipeline_records_each_call")


def test_make_client_returns_none_without_api_key():
    with _no_credentials():
        assert make_client() is None
    print("PASS test_make_client_returns_none_without_api_key")


def test_make_client_accepts_auth_token():
    # ANTHROPIC_AUTH_TOKEN (e.g. an `ant auth login` bearer token) is a
    # second static credential the SDK itself resolves -- make_client()
    # must not report "unavailable" just because ANTHROPIC_API_KEY specifically
    # is unset.
    with _no_credentials():
        os.environ["ANTHROPIC_AUTH_TOKEN"] = "fake-token-for-test"
        try:
            client = make_client()
        finally:
            del os.environ["ANTHROPIC_AUTH_TOKEN"]
        assert client is not None
    print("PASS test_make_client_accepts_auth_token")


if __name__ == "__main__":
    test_analyse_vacancy_valid_response()
    test_analyse_vacancy_truncated_raises()
    test_analyse_vacancy_genuinely_truncated_raises()
    test_analyse_vacancy_no_client_raises()
    test_tailor_application_valid_response()
    test_tailor_application_truncated_raises()
    test_tailor_application_genuinely_truncated_raises()
    test_tailor_application_no_client_raises()
    test_tailor_application_propagates_sdk_errors()
    test_make_client_returns_none_without_api_key()
    test_make_client_accepts_auth_token()
    test_allowed_terms_includes_company_and_keywords()
    test_allowed_terms_splits_multi_word_keywords()
    test_allowed_terms_skips_missing_company()
    test_analyse_vacancy_caps_vacancy_text_at_desc_chars()
    test_tailor_application_caps_description_at_desc_chars()
    test_tailor_application_includes_retry_errors_in_prompt()
    test_parse_or_raise_translates_payment_required_to_credit_balance_error()
    test_parse_or_raise_does_not_misclassify_an_ordinary_bad_request_error()
    test_run_tailor_pipeline_no_retry_when_first_reply_is_valid()
    test_run_tailor_pipeline_retries_once_then_succeeds()
    test_run_tailor_pipeline_returns_remaining_errors_when_retry_also_fails()
    test_tailor_application_caches_only_the_facts_block()
    test_tailor_application_facts_block_is_stable_across_calls()
    test_analyse_vacancy_records_only_when_job_id_and_store_given()
    test_tailor_application_records_cache_tokens()
    test_record_store_failure_does_not_break_generation()
    test_run_tailor_pipeline_records_each_call()
    print("\nAll tailor tests passed.")
