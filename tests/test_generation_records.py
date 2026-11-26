"""Offline tests for jobtracker/generation_records.py (issue #10, extended by
issue #13 for prompt-cache token fields + cost tracking): the local JSON
backend and the response -> record field extraction. No database, no
network."""
import datetime
import shutil
import tempfile
from pathlib import Path

from jobtracker.generation_records import (
    LocalJSONGenerationRecordStore,
    cost_for_record,
    monthly_cost_summary,
    record_from_response,
)


class _FakeUsage:
    def __init__(self, input_tokens, output_tokens, cache_creation_input_tokens=None,
                cache_read_input_tokens=None):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cache_creation_input_tokens = cache_creation_input_tokens
        self.cache_read_input_tokens = cache_read_input_tokens


class _FakeResponse:
    def __init__(self, stop_reason="end_turn", input_tokens=100, output_tokens=200,
                usage=True, cache_creation_input_tokens=None, cache_read_input_tokens=None):
        self.stop_reason = stop_reason
        self.usage = (_FakeUsage(input_tokens, output_tokens, cache_creation_input_tokens,
                                 cache_read_input_tokens) if usage else None)
        self.model = "fake-model"

    def to_dict(self):
        return {"stop_reason": self.stop_reason, "model": self.model}


def test_record_from_response_extracts_fields():
    rec = record_from_response("job:1", "tailor", "claude-sonnet-5", _FakeResponse())
    assert rec["job_id"] == "job:1"
    assert rec["stage"] == "tailor"
    assert rec["model"] == "claude-sonnet-5"
    assert rec["stop_reason"] == "end_turn"
    assert rec["input_tokens"] == 100
    assert rec["output_tokens"] == 200
    assert rec["raw_response"] == {"stop_reason": "end_turn", "model": "fake-model"}
    print("PASS test_record_from_response_extracts_fields")


def test_record_from_response_handles_missing_usage():
    rec = record_from_response("job:1", "analyse", "claude-haiku-4-5-20251001",
                               _FakeResponse(usage=False))
    assert rec["input_tokens"] is None and rec["output_tokens"] is None
    print("PASS test_record_from_response_handles_missing_usage")


def test_local_json_store_round_trips_and_filters_by_job():
    tmp = Path(tempfile.mkdtemp(prefix="jt_genrec_"))
    try:
        store = LocalJSONGenerationRecordStore(str(tmp / "generation_records.json"))
        store.record("job:1", "analyse", "claude-haiku-4-5-20251001", "end_turn", 50, 80,
                    {"a": 1})
        store.record("job:1", "tailor", "claude-sonnet-5", "end_turn", 500, 900, {"b": 2})
        store.record("job:2", "analyse", "claude-haiku-4-5-20251001", "end_turn", 50, 80,
                    {"c": 3})

        job1 = store.list_for_job("job:1")
        assert len(job1) == 2
        assert {r["stage"] for r in job1} == {"analyse", "tailor"}
        assert all(r["job_id"] == "job:1" for r in job1)

        job2 = store.list_for_job("job:2")
        assert len(job2) == 1

        # a fresh store instance reading the same file sees the same rows
        reloaded = LocalJSONGenerationRecordStore(str(tmp / "generation_records.json"))
        assert len(reloaded.list_for_job("job:1")) == 2
        print("PASS test_local_json_store_round_trips_and_filters_by_job")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_local_json_store_generates_unique_record_ids():
    tmp = Path(tempfile.mkdtemp(prefix="jt_genrec_"))
    try:
        store = LocalJSONGenerationRecordStore(str(tmp / "generation_records.json"))
        r1 = store.record("job:1", "analyse", "m", "end_turn", 1, 1, {})
        r2 = store.record("job:1", "analyse", "m", "end_turn", 1, 1, {})
        assert r1["record_id"] != r2["record_id"]
        print("PASS test_local_json_store_generates_unique_record_ids")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------- #
# issue #13: cache token extraction, list_all, cost tracking                  #
# --------------------------------------------------------------------------- #
def test_record_from_response_extracts_cache_tokens():
    rec = record_from_response("job:1", "tailor", "claude-sonnet-5",
                               _FakeResponse(cache_creation_input_tokens=500,
                                            cache_read_input_tokens=50))
    assert rec["cache_creation_input_tokens"] == 500
    assert rec["cache_read_input_tokens"] == 50
    print("PASS test_record_from_response_extracts_cache_tokens")


def test_record_from_response_cache_tokens_default_none():
    rec = record_from_response("job:1", "analyse", "claude-haiku-4-5-20251001", _FakeResponse())
    assert rec["cache_creation_input_tokens"] is None
    assert rec["cache_read_input_tokens"] is None
    print("PASS test_record_from_response_cache_tokens_default_none")


def test_local_json_store_list_all_returns_every_job():
    tmp = Path(tempfile.mkdtemp(prefix="jt_genrec_"))
    try:
        store = LocalJSONGenerationRecordStore(str(tmp / "generation_records.json"))
        store.record("job:1", "analyse", "m", "end_turn", 1, 1, {})
        store.record("job:2", "tailor", "m", "end_turn", 1, 1, {})
        assert len(store.list_all()) == 2
        print("PASS test_local_json_store_list_all_returns_every_job")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_cost_for_record_computes_cache_aware_cost():
    # claude-sonnet-5: $3/$15 per 1M (input/output); cache write 1.25x,
    # cache read 0.1x the base INPUT price (current pricing, see the module
    # docstring/plan doc for the source and date).
    record = {
        "model": "claude-sonnet-5",
        "input_tokens": 1_000_000, "output_tokens": 1_000_000,
        "cache_creation_input_tokens": 1_000_000, "cache_read_input_tokens": 1_000_000,
    }
    cost = cost_for_record(record)
    # 3.00 (input) + 15.00 (output) + 3.00*1.25 (cache write) + 3.00*0.1 (cache read)
    assert abs(cost - (3.00 + 15.00 + 3.75 + 0.30)) < 1e-9
    print("PASS test_cost_for_record_computes_cache_aware_cost")


def test_cost_for_record_unknown_model_returns_none():
    record = {"model": "some-future-model", "input_tokens": 100, "output_tokens": 100}
    assert cost_for_record(record) is None
    print("PASS test_cost_for_record_unknown_model_returns_none")


def test_cost_for_record_tolerates_missing_token_fields():
    # A row recorded before cache fields existed (or with a missing usage
    # object) must still price -- missing fields are 0, not a crash.
    record = {"model": "claude-haiku-4-5-20251001", "input_tokens": None, "output_tokens": 200}
    # 200 output tokens * $5/1M, no input/cache contribution
    assert abs(cost_for_record(record) - (200 * 5.00 / 1_000_000)) < 1e-12
    print("PASS test_cost_for_record_tolerates_missing_token_fields")


def test_monthly_cost_summary_groups_by_month_newest_first():
    records = [
        {"model": "claude-sonnet-5", "input_tokens": 1000, "output_tokens": 500,
         "created_at": "2026-10-15T10:00:00+00:00"},
        {"model": "claude-haiku-4-5-20251001", "input_tokens": 2000, "output_tokens": 300,
         "created_at": "2026-11-01T09:00:00+00:00"},
        {"model": "claude-sonnet-5", "input_tokens": 500, "output_tokens": 250,
         "created_at": "2026-11-20T09:00:00+00:00"},
    ]
    months = monthly_cost_summary(records)
    assert [m["month"] for m in months] == ["2026-11", "2026-10"]
    assert months[0]["calls"] == 2
    assert months[1]["calls"] == 1
    assert months[0]["cost_usd"] > 0
    assert set(months[0]["by_model"].keys()) == {"claude-sonnet-5", "claude-haiku-4-5-20251001"}
    print("PASS test_monthly_cost_summary_groups_by_month_newest_first")


def test_monthly_cost_summary_counts_unpriced_calls_separately():
    records = [
        {"model": "unknown-model", "input_tokens": 1000, "output_tokens": 500,
         "created_at": "2026-11-01T09:00:00+00:00"},
    ]
    months = monthly_cost_summary(records)
    assert months[0]["unpriced_calls"] == 1
    assert months[0]["cost_usd"] == 0.0
    print("PASS test_monthly_cost_summary_counts_unpriced_calls_separately")


def test_monthly_cost_summary_handles_datetime_created_at():
    # The Postgres store returns `created_at` as a real datetime object
    # (psycopg2 decodes TIMESTAMPTZ), not the isoformat() string the local
    # JSON store writes -- both must group correctly.
    records = [
        {"model": "claude-sonnet-5", "input_tokens": 100, "output_tokens": 50,
         "created_at": datetime.datetime(2026, 11, 20, 9, 0, tzinfo=datetime.timezone.utc)},
    ]
    months = monthly_cost_summary(records)
    assert months[0]["month"] == "2026-11"
    print("PASS test_monthly_cost_summary_handles_datetime_created_at")


def test_monthly_cost_summary_converts_non_utc_datetime_to_utc_month():
    # psycopg2 decodes TIMESTAMPTZ into the CONNECTION's session timezone,
    # not necessarily UTC -- a naive strftime("%Y-%m") on that value groups
    # by the WRONG month whenever the session timezone and the moment both
    # sit near a month boundary. This instant is 2026-11-30T23:30 UTC, but
    # 2026-12-01T00:30 in UTC+1 -- the two backends must still agree it's
    # November.
    tz_plus_one = datetime.timezone(datetime.timedelta(hours=1))
    records = [
        {"model": "claude-sonnet-5", "input_tokens": 100, "output_tokens": 50,
         "created_at": datetime.datetime(2026, 12, 1, 0, 30, tzinfo=tz_plus_one)},
    ]
    months = monthly_cost_summary(records)
    assert months[0]["month"] == "2026-11"
    print("PASS test_monthly_cost_summary_converts_non_utc_datetime_to_utc_month")


def test_monthly_cost_summary_marks_unpriced_models_in_by_model():
    # A model missing from MODEL_PRICING must render distinguishably from
    # an actually-free model in the per-model breakdown too, not just at
    # the month-level unpriced_calls count.
    records = [
        {"model": "claude-sonnet-5", "input_tokens": 100, "output_tokens": 50,
         "created_at": "2026-11-01T09:00:00+00:00"},
        {"model": "some-future-model", "input_tokens": 100, "output_tokens": 50,
         "created_at": "2026-11-01T09:00:00+00:00"},
    ]
    months = monthly_cost_summary(records)
    by_model = months[0]["by_model"]
    assert by_model["claude-sonnet-5"]["unpriced"] is False
    assert by_model["some-future-model"]["unpriced"] is True
    assert by_model["some-future-model"]["cost_usd"] == 0.0
    print("PASS test_monthly_cost_summary_marks_unpriced_models_in_by_model")


def test_local_json_store_record_is_thread_safe_under_concurrent_writes():
    # webapp.py constructs a FRESH store instance per request -- a race
    # between two concurrent generations (two background-thread workers)
    # writing through DIFFERENT instances pointed at the SAME file must not
    # drop a row (non-atomic read-modify-write) or ever leave the file
    # truncated/corrupt for a /costs request reading it mid-write.
    import threading as _threading

    tmp = Path(tempfile.mkdtemp(prefix="jt_genrec_"))
    try:
        path = str(tmp / "generation_records.json")
        n_threads, n_per_thread = 8, 10

        def _write_many():
            store = LocalJSONGenerationRecordStore(path)  # own instance, same file
            for _ in range(n_per_thread):
                store.record("job:x", "tailor", "claude-sonnet-5", "end_turn", 1, 1, {})

        threads = [_threading.Thread(target=_write_many) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        final = LocalJSONGenerationRecordStore(path).list_all()
        assert len(final) == n_threads * n_per_thread
        assert len({r["record_id"] for r in final}) == n_threads * n_per_thread
        print("PASS test_local_json_store_record_is_thread_safe_under_concurrent_writes")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    test_record_from_response_extracts_fields()
    test_record_from_response_handles_missing_usage()
    test_local_json_store_round_trips_and_filters_by_job()
    test_local_json_store_generates_unique_record_ids()
    test_record_from_response_extracts_cache_tokens()
    test_record_from_response_cache_tokens_default_none()
    test_local_json_store_list_all_returns_every_job()
    test_cost_for_record_computes_cache_aware_cost()
    test_cost_for_record_unknown_model_returns_none()
    test_cost_for_record_tolerates_missing_token_fields()
    test_monthly_cost_summary_groups_by_month_newest_first()
    test_monthly_cost_summary_counts_unpriced_calls_separately()
    test_monthly_cost_summary_handles_datetime_created_at()
    test_monthly_cost_summary_converts_non_utc_datetime_to_utc_month()
    test_monthly_cost_summary_marks_unpriced_models_in_by_model()
    test_local_json_store_record_is_thread_safe_under_concurrent_writes()
    print("\nAll generation_records tests passed.")
