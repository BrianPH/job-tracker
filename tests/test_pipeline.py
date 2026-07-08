"""Offline tests: parse real Recruitee payload, filter, and upsert to local store.

Run:  python -m tests.test_pipeline   (from the job-tracker/ dir)
No network required — uses the saved GenomeScan fixture.
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jobtracker.adapters import parse_recruitee_offer
from jobtracker.filters import apply_rules
from jobtracker.models import Job, html_to_text
from jobtracker.storage import LocalJSONStore

FIXTURE = Path(__file__).parent / "fixtures" / "genomescan_offers.json"


def load_offers():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))["offers"]


def test_parse_recruitee():
    offers = load_offers()
    jobs = [parse_recruitee_offer(o, "GenomeScan") for o in offers]
    assert len(jobs) == 3
    j = jobs[0]
    assert j.title == "Director Commercial Laboratory"
    assert j.company == "GenomeScan B.V."
    assert j.source == "Recruitee"
    assert j.location == "Leiden, Zuid-Holland, Netherlands"
    assert j.remote is False
    assert j.job_id == "recruitee:54y7n"
    assert j.posted_date == "2026-04-21"
    assert j.url.endswith("/director-commercial-laboratory")
    # HTML stripped, requirements appended
    assert "<p>" not in j.description
    assert "Director Commercial Laboratory" in j.description
    assert "Master" in j.description  # from requirements block
    print("PASS test_parse_recruitee")


def test_html_cleaner():
    assert html_to_text("<p>Hello&nbsp;<strong>world</strong></p>") == "Hello world"
    assert "• a" in html_to_text("<ul><li>a</li><li>b</li></ul>")
    print("PASS test_html_cleaner")


def test_dedup_id_stable():
    o = load_offers()[0]
    a = parse_recruitee_offer(o, "GenomeScan")
    b = parse_recruitee_offer(o, "GenomeScan")
    assert a.job_id == b.job_id
    print("PASS test_dedup_id_stable")


def test_filter_keeps_relevant():
    jobs = [parse_recruitee_offer(o, "GenomeScan") for o in load_offers()]
    profile = {"rules": {
        "keywords_any": ["sequencing", "bio-it", "ict"],
        "exclude_any": ["internship"],
        "locations_any": ["Leiden"],
        "always_keep_remote": True,
    }}
    kept = apply_rules(jobs, profile)
    titles = {j.title for j in kept}
    # Director role mentions Next Generation Sequencing; Bio-IT matches; both in Leiden
    assert "Director Commercial Laboratory" in titles
    assert "Open Application - Bio-IT or ICT" in titles
    print(f"PASS test_filter_keeps_relevant ({len(kept)}/3 kept)")


def test_local_store_upsert():
    jobs = [parse_recruitee_offer(o, "GenomeScan") for o in load_offers()]
    with tempfile.TemporaryDirectory() as d:
        store = LocalJSONStore(str(Path(d) / "jobs.json"))
        r1 = store.upsert(jobs)
        assert r1 == {"added": 3, "updated": 0, "total": 3}
        # second run: no duplicates
        r2 = store.upsert(jobs)
        assert r2["added"] == 0 and r2["total"] == 3
        # status preserved across runs
        data = json.loads((Path(d) / "jobs.json").read_text())
        data_key = next(iter(data))
        data[data_key]["status"] = "Applied"
        (Path(d) / "jobs.json").write_text(json.dumps(data))
        store.upsert(jobs)
        data2 = json.loads((Path(d) / "jobs.json").read_text())
        assert data2[data_key]["status"] == "Applied"
    print("PASS test_local_store_upsert (dedup + status preserved)")


if __name__ == "__main__":
    test_html_cleaner()
    test_parse_recruitee()
    test_dedup_id_stable()
    test_filter_keeps_relevant()
    test_local_store_upsert()
    print("\nAll tests passed.")
