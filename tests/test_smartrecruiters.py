"""Offline tests for the smartrecruiters adapter (no real network: fetch_* tests
monkeypatch requests.Session.get with an in-memory fake)."""
from unittest.mock import MagicMock, patch

from jobtracker.adapters import (
    apply_smartrecruiters_detail,
    fetch_smartrecruiters,
    parse_smartrecruiters_posting,
)
from jobtracker.models import Job


def _posting(pid: str) -> dict:
    return {
        "id": pid,
        "name": f"Role {pid}",
        "company": {"identifier": "Acme", "name": "Acme"},
        "location": {"fullLocation": "Remote"},
    }


def test_parse_smartrecruiters_posting():
    item = {
        "id": "744000143139580",
        "name": "Principal Technical Project Manager",
        "company": {"identifier": "GenomicsEngland", "name": "Genomics England"},
        "releasedDate": "2026-08-12T16:03:50.720Z",
        "location": {
            "city": "London", "region": "England", "country": "gb",
            "remote": False, "hybrid": True,
            "fullLocation": "London, England, United Kingdom",
        },
    }
    job = parse_smartrecruiters_posting(item, "Genomics England")
    assert job.job_id == "smartrecruiters:744000143139580"
    assert job.title == "Principal Technical Project Manager"
    assert job.company == "Genomics England"
    assert job.location == "London, England, United Kingdom"
    assert not job.remote
    assert job.url == "https://jobs.smartrecruiters.com/GenomicsEngland/744000143139580"
    assert job.posted_date == "2026-08-12"
    print("PASS test_parse_smartrecruiters_posting")


def test_parse_smartrecruiters_posting_falls_back_to_company_param():
    # When a posting row omits company.identifier, the URL must fall back to the
    # company id passed to fetch_smartrecruiters rather than leaving it blank
    # (which would otherwise produce a malformed "...com//<id>" URL).
    item = {"id": "42", "name": "Role", "company": {"name": "Acme"}}
    job = parse_smartrecruiters_posting(item, "Acme", company_id="Acme")
    assert job.url == "https://jobs.smartrecruiters.com/Acme/42"
    print("PASS test_parse_smartrecruiters_posting_falls_back_to_company_param")


def test_apply_smartrecruiters_detail():
    job = Job(title="Role", company="Acme", source="SmartRecruiters",
              url="https://jobs.smartrecruiters.com/Acme/1", job_id="smartrecruiters:1")
    info = {
        "postingUrl": "https://jobs.smartrecruiters.com/Acme/1-principal-role",
        "jobAd": {"sections": {
            "jobDescription": {"text": "<p>Build genomics pipelines.</p>"},
            "qualifications": {"text": "<p>5+ years Python.</p>"},
        }},
    }
    apply_smartrecruiters_detail(job, info)
    assert job.url == "https://jobs.smartrecruiters.com/Acme/1-principal-role"
    assert "Build genomics pipelines" in job.description
    assert "5+ years Python" in job.description
    print("PASS test_apply_smartrecruiters_detail")


def test_apply_smartrecruiters_detail_empty_is_noop():
    job = Job(title="Role", company="Acme", source="SmartRecruiters",
              url="https://jobs.smartrecruiters.com/Acme/1", job_id="smartrecruiters:1")
    apply_smartrecruiters_detail(job, {})
    assert job.url == "https://jobs.smartrecruiters.com/Acme/1"
    assert job.description == ""
    print("PASS test_apply_smartrecruiters_detail_empty_is_noop")


def test_fetch_smartrecruiters_paginates_across_pages():
    pages = {
        0: {"totalFound": 150, "content": [_posting(str(i)) for i in range(100)]},
        100: {"totalFound": 150, "content": [_posting(str(i)) for i in range(100, 150)]},
    }
    list_calls = []

    def fake_get(self, url, params=None, timeout=None):
        resp = MagicMock()
        resp.raise_for_status = lambda: None
        list_calls.append((params["offset"], params["limit"]))
        resp.json = lambda: pages[params["offset"]]
        return resp

    with patch("jobtracker.adapters.requests.Session.get", fake_get):
        jobs = fetch_smartrecruiters("Acme", {"company": "Acme", "max_results": 150,
                                               "descriptions": False})

    assert list_calls == [(0, 100), (100, 50)]
    assert len(jobs) == 150
    print("PASS test_fetch_smartrecruiters_paginates_across_pages")


def test_fetch_smartrecruiters_caps_detail_fetches_at_max_results():
    # A page can return more rows than requested (e.g. the API ignoring `limit`);
    # detail fetches must stop once max_results jobs have been collected.
    page = {"totalFound": 3, "content": [_posting(str(i)) for i in range(3)]}
    list_calls, detail_calls = [], []

    def fake_get(self, url, params=None, timeout=None):
        resp = MagicMock()
        resp.raise_for_status = lambda: None
        if params is not None:
            list_calls.append((params["offset"], params["limit"]))
            resp.json = lambda: page
        else:
            detail_calls.append(url)
            resp.json = lambda: {}
        return resp

    with patch("jobtracker.adapters.requests.Session.get", fake_get):
        jobs = fetch_smartrecruiters("Acme", {"company": "Acme", "max_results": 2,
                                               "descriptions": True})

    assert len(jobs) == 2
    assert len(detail_calls) == 2  # not 3 -- the third row must not be detail-fetched
    print("PASS test_fetch_smartrecruiters_caps_detail_fetches_at_max_results")


def test_fetch_smartrecruiters_stops_on_empty_page():
    def fake_get(self, url, params=None, timeout=None):
        resp = MagicMock()
        resp.raise_for_status = lambda: None
        resp.json = lambda: {"totalFound": 0, "content": []}
        return resp

    with patch("jobtracker.adapters.requests.Session.get", fake_get):
        jobs = fetch_smartrecruiters("Acme", {"company": "Acme"})

    assert jobs == []
    print("PASS test_fetch_smartrecruiters_stops_on_empty_page")


if __name__ == "__main__":
    test_parse_smartrecruiters_posting()
    test_parse_smartrecruiters_posting_falls_back_to_company_param()
    test_apply_smartrecruiters_detail()
    test_apply_smartrecruiters_detail_empty_is_noop()
    test_fetch_smartrecruiters_paginates_across_pages()
    test_fetch_smartrecruiters_caps_detail_fetches_at_max_results()
    test_fetch_smartrecruiters_stops_on_empty_page()
    print("\nAll smartrecruiters adapter tests passed.")
