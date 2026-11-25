"""Offline tests for jobtracker/ingest.py (no real network: requests.get is
monkeypatched with an in-memory fake, matching the pattern in
tests/test_smartrecruiters.py)."""
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from jobtracker.ingest import ThinContentError, detect_platform, ingest_url
from jobtracker.snapshots import LocalJSONSnapshotStore

FIXTURES = Path(__file__).resolve().parent / "fixtures"


class FakeSnapshotStore:
    """Records save() calls in memory so tests can assert on them without touching disk."""
    def __init__(self):
        self.saved = []

    def save(self, url, job_id="", platform="", raw_source="", extracted_text=""):
        row = {"url": url, "job_id": job_id, "platform": platform,
               "raw_source": raw_source, "extracted_text": extracted_text}
        self.saved.append(row)
        return {"snapshot_id": "fake", "url": url}

    def get(self, url):
        for row in reversed(self.saved):
            if row["url"] == url:
                return row
        return None


def _fake_get(responses):
    """responses: dict url-substring -> (json_body or None, text_body or "")."""
    def fake_get(url, headers=None, timeout=None, **kwargs):
        for needle, (json_body, text_body) in responses.items():
            if needle in url:
                resp = MagicMock()
                resp.raise_for_status = lambda: None
                resp.json = (lambda jb=json_body: jb) if json_body is not None else (lambda: {})
                resp.text = text_body
                return resp
        raise AssertionError(f"unexpected URL in test: {url}")
    return fake_get


# --------------------------------------------------------------------------- #
# Platform detection                                                           #
# --------------------------------------------------------------------------- #
def test_detect_platform():
    cases = {
        "https://job-boards.greenhouse.io/acme/jobs/1234567": "greenhouse",
        "https://jobs.lever.co/acme/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee": "lever",
        "https://jobs.ashbyhq.com/mollie/d056f390-1d70-408e-8f85-2360d41eaa84": "ashby",
        "https://jobs.smartrecruiters.com/Acme/744000143139580": "smartrecruiters",
        "https://acme.teamtailor.com/jobs/123456-data-engineer": "teamtailor",
        "https://amgen.wd1.myworkdayjobs.com/Careers/job/NL/Role_R-200123": "workday",
        "https://acme.recruitee.com/o/data-engineer": "recruitee",
        "https://example.com/careers/data-engineer": None,
    }
    for url, expected in cases.items():
        assert detect_platform(url) == expected, f"{url} -> expected {expected}"
    print("PASS test_detect_platform")


# --------------------------------------------------------------------------- #
# Known-platform detail re-fetch                                               #
# --------------------------------------------------------------------------- #
def test_ingest_greenhouse_detail():
    url = "https://job-boards.greenhouse.io/acme/jobs/1234567"
    detail = {
        "title": "Data Engineer", "absolute_url": url,
        "location": {"name": "Amsterdam"}, "content": "<p>Build pipelines.</p>",
        "updated_at": "2026-01-10T00:00:00Z",
    }
    store = FakeSnapshotStore()
    with patch("jobtracker.ingest.requests.get", _fake_get({"boards-api.greenhouse.io": (detail, "{}")})):
        job = ingest_url(url, snapshot_store=store)
    assert job["title"] == "Data Engineer"
    assert job["source"] == "Greenhouse"
    assert "Build pipelines" in job["description"]
    assert store.saved[0]["platform"] == "greenhouse"
    print("PASS test_ingest_greenhouse_detail")


def test_ingest_lever_detail():
    url = "https://jobs.lever.co/acme/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    detail = {
        "text": "Backend Engineer", "hostedUrl": url,
        "categories": {"location": "Remote"}, "descriptionPlain": "Build APIs.",
        "id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
    }
    store = FakeSnapshotStore()
    with patch("jobtracker.ingest.requests.get", _fake_get({"api.lever.co": (detail, "{}")})):
        job = ingest_url(url, snapshot_store=store)
    assert job["title"] == "Backend Engineer"
    assert job["source"] == "Lever"
    assert store.saved[0]["platform"] == "lever"
    print("PASS test_ingest_lever_detail")


def test_ingest_ashby_detail():
    url = "https://jobs.ashbyhq.com/mollie/d056f390-1d70-408e-8f85-2360d41eaa84"
    board = {"jobs": [
        {"id": "other-id", "title": "Other role"},
        {"id": "d056f390-1d70-408e-8f85-2360d41eaa84", "title": "Support Engineer",
         "jobUrl": url, "descriptionHtml": "<p>Help customers.</p>"},
    ]}
    store = FakeSnapshotStore()
    with patch("jobtracker.ingest.requests.get", _fake_get({"api.ashbyhq.com": (board, "{}")})):
        job = ingest_url(url, snapshot_store=store)
    assert job["title"] == "Support Engineer"
    assert job["job_id"] == "ashby:d056f390-1d70-408e-8f85-2360d41eaa84"
    assert store.saved[0]["platform"] == "ashby"
    print("PASS test_ingest_ashby_detail")


def test_ingest_smartrecruiters_detail():
    url = "https://jobs.smartrecruiters.com/Acme/744000143139580"
    detail = {
        "id": "744000143139580", "name": "Principal Engineer",
        "company": {"identifier": "Acme", "name": "Acme"},
        "location": {"fullLocation": "London"},
        "postingUrl": url,
        "jobAd": {"sections": {"jobDescription": {"text": "<p>Lead the platform.</p>"}}},
    }
    store = FakeSnapshotStore()
    with patch("jobtracker.ingest.requests.get",
              _fake_get({"api.smartrecruiters.com": (detail, "{}")})):
        job = ingest_url(url, snapshot_store=store)
    assert job["title"] == "Principal Engineer"
    assert job["company"] == "Acme"
    assert "Lead the platform" in job["description"]
    assert store.saved[0]["platform"] == "smartrecruiters"
    print("PASS test_ingest_smartrecruiters_detail")


def test_ingest_teamtailor_detail():
    url = "https://acme.teamtailor.com/jobs/123456-data-engineer"
    listing = {"items": [
        {"id": "x", "title": "Other role", "url": "https://acme.teamtailor.com/jobs/999999-other"},
        {"id": "y", "title": "Data Engineer", "url": url, "content_text": "Build data pipelines."},
    ]}
    store = FakeSnapshotStore()
    with patch("jobtracker.ingest.requests.get", _fake_get({"acme.teamtailor.com/jobs.json": (listing, "{}")})):
        job = ingest_url(url, snapshot_store=store)
    assert job["title"] == "Data Engineer"
    assert store.saved[0]["platform"] == "teamtailor"
    print("PASS test_ingest_teamtailor_detail")


def test_ingest_workday_detail():
    url = "https://amgen.wd1.myworkdayjobs.com/Careers/job/Netherlands-Breda/Scientist_R-200123"
    detail = {"jobPostingInfo": {
        "title": "Scientist, Bioinformatics",
        "jobDescription": "<p>Build NGS pipelines.</p>",
        "location": "Netherlands - Breda",
    }}
    store = FakeSnapshotStore()
    with patch("jobtracker.ingest.requests.get",
              _fake_get({"amgen.wd1.myworkdayjobs.com/wday/cxs": (detail, "{}")})):
        job = ingest_url(url, snapshot_store=store)
    assert job["title"] == "Scientist, Bioinformatics"
    assert job["source"] == "Workday"
    assert "Build NGS pipelines" in job["description"]
    assert store.saved[0]["platform"] == "workday"
    print("PASS test_ingest_workday_detail")


def test_ingest_recruitee_detail():
    url = "https://acme.recruitee.com/o/data-engineer"
    detail = {"offer": {
        "title": "Data Engineer", "careers_url": url,
        "description": "<p>Build data infra.</p>", "guid": "abc123",
    }}
    store = FakeSnapshotStore()
    with patch("jobtracker.ingest.requests.get", _fake_get({"acme.recruitee.com/api/offers": (detail, "{}")})):
        job = ingest_url(url, snapshot_store=store)
    assert job["title"] == "Data Engineer"
    assert store.saved[0]["platform"] == "recruitee"
    print("PASS test_ingest_recruitee_detail")


# --------------------------------------------------------------------------- #
# Generic fallback (trafilatura) + pasted text + thin-content                 #
# --------------------------------------------------------------------------- #
def test_ingest_generic_page_uses_trafilatura():
    url = "https://example-institute.org/careers/research-software-engineer"
    html = (FIXTURES / "generic_vacancy.html").read_text(encoding="utf-8")
    store = FakeSnapshotStore()
    with patch("jobtracker.ingest.requests.get", _fake_get({"example-institute.org": (None, html)})):
        job = ingest_url(url, snapshot_store=store)
    assert job["source"] == "Ingested"
    assert "Research Software Engineer" in job["title"]
    assert "genomic" in job["description"].lower()
    # chrome stripped by trafilatura
    assert "cookie" not in job["description"].lower()
    assert store.saved[0]["platform"] == "html"
    assert store.saved[0]["raw_source"] == html
    print("PASS test_ingest_generic_page_uses_trafilatura")


def test_ingest_pasted_text_skips_network():
    store = FakeSnapshotStore()
    text = "Senior Developer at Acme. " + ("Relevant responsibilities and requirements. " * 10).strip()
    job = ingest_url("https://example.com/vacancy/1", paste_text=text, snapshot_store=store)
    assert job["source"] == "Pasted"
    assert job["description"] == text
    assert store.saved[0]["platform"] == "paste"
    print("PASS test_ingest_pasted_text_skips_network")


def test_ingest_thin_content_raises():
    url = "https://spa-example.com/careers/1"
    html = "<html><body><div id='app'></div></body></html>"
    with patch("jobtracker.ingest.requests.get", _fake_get({"spa-example.com": (None, html)})):
        try:
            ingest_url(url, snapshot_store=FakeSnapshotStore())
        except ThinContentError as e:
            assert e.url == url
        else:
            raise AssertionError("expected ThinContentError")
    print("PASS test_ingest_thin_content_raises")


def test_ingest_requires_url_or_paste():
    try:
        ingest_url("", snapshot_store=FakeSnapshotStore())
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError")
    print("PASS test_ingest_requires_url_or_paste")


# --------------------------------------------------------------------------- #
# vacancy_snapshots durability: write, then read back as if the original      #
# posting had since been removed (the store never re-fetches the live URL).   #
# --------------------------------------------------------------------------- #
def test_snapshot_survives_posting_removed():
    tmp = Path(tempfile.mkdtemp(prefix="jt_snap_")) / "vacancy_snapshots.json"
    store = LocalJSONSnapshotStore(str(tmp))
    url = "https://example.com/careers/now-offline"
    store.save(url, job_id="html:abc", platform="html",
              raw_source="<html>...</html>", extracted_text="Full vacancy text here.")

    # Simulate the posting being gone: a fresh store instance reading from disk,
    # with no network access at all, must still return the saved snapshot.
    reloaded = LocalJSONSnapshotStore(str(tmp))
    row = reloaded.get(url)
    assert row is not None
    assert row["extracted_text"] == "Full vacancy text here."
    assert row["job_id"] == "html:abc"
    print("PASS test_snapshot_survives_posting_removed")


if __name__ == "__main__":
    test_detect_platform()
    test_ingest_greenhouse_detail()
    test_ingest_lever_detail()
    test_ingest_ashby_detail()
    test_ingest_smartrecruiters_detail()
    test_ingest_teamtailor_detail()
    test_ingest_workday_detail()
    test_ingest_recruitee_detail()
    test_ingest_generic_page_uses_trafilatura()
    test_ingest_pasted_text_skips_network()
    test_ingest_thin_content_raises()
    test_ingest_requires_url_or_paste()
    test_snapshot_survives_posting_removed()
    print("\nAll ingest tests passed.")
