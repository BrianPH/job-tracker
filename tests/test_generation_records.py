"""Offline tests for jobtracker/generation_records.py (issue #10): the local
JSON backend and the response -> record field extraction. No database, no
network."""
import shutil
import tempfile
from pathlib import Path

from jobtracker.generation_records import LocalJSONGenerationRecordStore, record_from_response


class _FakeUsage:
    def __init__(self, input_tokens, output_tokens):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class _FakeResponse:
    def __init__(self, stop_reason="end_turn", input_tokens=100, output_tokens=200,
                usage=True):
        self.stop_reason = stop_reason
        self.usage = _FakeUsage(input_tokens, output_tokens) if usage else None
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


if __name__ == "__main__":
    test_record_from_response_extracts_fields()
    test_record_from_response_handles_missing_usage()
    test_local_json_store_round_trips_and_filters_by_job()
    test_local_json_store_generates_unique_record_ids()
    print("\nAll generation_records tests passed.")
