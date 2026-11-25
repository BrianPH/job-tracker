"""Offline tests for jobtracker/ingest.py (no real network: requests.get is
monkeypatched with an in-memory fake, matching the pattern in
tests/test_smartrecruiters.py)."""
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import requests

from jobtracker.ingest import (
    IngestInputError,
    ThinContentError,
    _clean_title,
    _company_from_domain,
    detect_platform,
    ingest_url,
)
from jobtracker.snapshots import LocalJSONSnapshotStore, snapshot_id

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


def _fake_get(responses, calls=None):
    """responses: dict EXACT url -> (json_body or None, text_body, status_or_none).
    `calls`, if given, collects every requested URL so tests can assert on it."""
    def fake_get(url, headers=None, timeout=None, **kwargs):
        if calls is not None:
            calls.append(url)
        if url not in responses:
            raise AssertionError(f"unexpected URL in test: {url}\nexpected one of: "
                                 f"{sorted(responses)}")
        json_body, text_body, status = responses[url]
        resp = MagicMock()
        if status and status >= 400:
            resp.raise_for_status = MagicMock(side_effect=requests.HTTPError(f"{status} error"))
        else:
            resp.raise_for_status = lambda: None
        resp.json = (lambda jb=json_body: jb) if json_body is not None else (lambda: {})
        resp.text = text_body
        return resp
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
# Known-platform detail re-fetch: each test asserts the EXACT detail URL      #
# that was requested, not just a substring -- this is what catches a regex    #
# that extracts the wrong path (e.g. dropping "/job" for Workday).            #
# --------------------------------------------------------------------------- #
def test_ingest_greenhouse_detail():
    url = "https://job-boards.greenhouse.io/acme/jobs/1234567"
    detail_url = "https://boards-api.greenhouse.io/v1/boards/acme/jobs/1234567?content=true"
    detail = {
        "id": 1234567, "title": "Data Engineer", "absolute_url": url,
        "location": {"name": "Amsterdam"}, "content": "<p>Build pipelines.</p>",
        "updated_at": "2026-01-10T00:00:00Z",
    }
    store, calls = FakeSnapshotStore(), []
    with patch("jobtracker.ingest.requests.get", _fake_get({detail_url: (detail, "{}", None)}, calls)):
        job = ingest_url(url, snapshot_store=store)
    assert calls == [detail_url]
    assert job["title"] == "Data Engineer"
    assert job["source"] == "Greenhouse"
    assert job["job_id"] == "greenhouse:1234567"
    assert "Build pipelines" in job["description"]
    assert store.saved[0]["platform"] == "greenhouse"
    print("PASS test_ingest_greenhouse_detail")


def test_ingest_lever_detail():
    url = "https://jobs.lever.co/acme/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    detail_url = "https://api.lever.co/v0/postings/acme/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee?mode=json"
    detail = {
        "id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        "text": "Backend Engineer", "hostedUrl": url,
        "categories": {"location": "Remote"}, "descriptionPlain": "Build APIs.",
    }
    store, calls = FakeSnapshotStore(), []
    with patch("jobtracker.ingest.requests.get", _fake_get({detail_url: (detail, "{}", None)}, calls)):
        job = ingest_url(url, snapshot_store=store)
    assert calls == [detail_url]
    assert job["title"] == "Backend Engineer"
    assert job["source"] == "Lever"
    assert store.saved[0]["platform"] == "lever"
    print("PASS test_ingest_lever_detail")


def test_ingest_ashby_detail():
    url = "https://jobs.ashbyhq.com/mollie/d056f390-1d70-408e-8f85-2360d41eaa84"
    board_url = "https://api.ashbyhq.com/posting-api/job-board/mollie"
    board = {"jobs": [
        {"id": "other-id", "title": "Other role"},
        {"id": "d056f390-1d70-408e-8f85-2360d41eaa84", "title": "Support Engineer",
         "jobUrl": url, "descriptionHtml": "<p>Help customers.</p>"},
    ]}
    store, calls = FakeSnapshotStore(), []
    with patch("jobtracker.ingest.requests.get", _fake_get({board_url: (board, "{}", None)}, calls)):
        job = ingest_url(url, snapshot_store=store)
    assert calls == [board_url]
    assert job["title"] == "Support Engineer"
    assert job["job_id"] == "ashby:d056f390-1d70-408e-8f85-2360d41eaa84"
    assert store.saved[0]["platform"] == "ashby"
    print("PASS test_ingest_ashby_detail")


def test_ingest_smartrecruiters_detail():
    url = "https://jobs.smartrecruiters.com/Acme/744000143139580"
    detail_url = "https://api.smartrecruiters.com/v1/companies/Acme/postings/744000143139580"
    detail = {
        "id": "744000143139580", "name": "Principal Engineer",
        "company": {"identifier": "Acme", "name": "Acme"},
        "location": {"fullLocation": "London"},
        "postingUrl": url,
        "jobAd": {"sections": {"jobDescription": {"text": "<p>Lead the platform.</p>"}}},
    }
    store, calls = FakeSnapshotStore(), []
    with patch("jobtracker.ingest.requests.get", _fake_get({detail_url: (detail, "{}", None)}, calls)):
        job = ingest_url(url, snapshot_store=store)
    assert calls == [detail_url]
    assert job["title"] == "Principal Engineer"
    assert job["company"] == "Acme"
    assert "Lead the platform" in job["description"]
    assert store.saved[0]["platform"] == "smartrecruiters"
    print("PASS test_ingest_smartrecruiters_detail")


def test_ingest_teamtailor_detail():
    url = "https://acme.teamtailor.com/jobs/123456-data-engineer"
    list_url = "https://acme.teamtailor.com/jobs.json"
    listing = {"items": [
        {"id": "x", "title": "Other role", "url": "https://acme.teamtailor.com/jobs/999999-other"},
        {"id": "y", "title": "Data Engineer", "url": url, "content_text": "Build data pipelines."},
    ]}
    store, calls = FakeSnapshotStore(), []
    with patch("jobtracker.ingest.requests.get", _fake_get({list_url: (listing, "{}", None)}, calls)):
        job = ingest_url(url, snapshot_store=store)
    assert calls == [list_url]
    assert job["title"] == "Data Engineer"
    assert store.saved[0]["platform"] == "teamtailor"
    print("PASS test_ingest_teamtailor_detail")


def test_ingest_workday_detail():
    # Regression test for the "/job" segment bug: a wrong regex dropped "/job"
    # from the captured external path, so the built CXS URL 404s against the
    # real API. Asserting the exact detail_url is what catches that class of
    # bug -- a substring match on just the host would pass either way.
    url = "https://amgen.wd1.myworkdayjobs.com/en-US/Careers/job/Netherlands-Breda/Scientist_R-200123"
    detail_url = ("https://amgen.wd1.myworkdayjobs.com/wday/cxs/amgen/Careers"
                  "/job/Netherlands-Breda/Scientist_R-200123")
    detail = {"jobPostingInfo": {
        "title": "Scientist, Bioinformatics",
        "jobDescription": "<p>Build NGS pipelines.</p>",
        "location": "Netherlands - Breda",
    }}
    store, calls = FakeSnapshotStore(), []
    with patch("jobtracker.ingest.requests.get", _fake_get({detail_url: (detail, "{}", None)}, calls)):
        job = ingest_url(url, snapshot_store=store)
    assert calls == [detail_url]
    assert job["title"] == "Scientist, Bioinformatics"
    assert job["source"] == "Workday"
    assert job["url"] == ("https://amgen.wd1.myworkdayjobs.com/Careers"
                          "/job/Netherlands-Breda/Scientist_R-200123")
    assert "Build NGS pipelines" in job["description"]
    assert store.saved[0]["platform"] == "workday"
    print("PASS test_ingest_workday_detail")


def test_ingest_workday_detail_strips_tracking_query_string():
    # Pasted vacancy URLs commonly carry apply-tracking query params; they
    # must not leak into the CXS detail URL or the job_id.
    url = ("https://amgen.wd1.myworkdayjobs.com/en-US/Careers/job/NL/"
          "Role_R-200123?source=LinkedIn&utm_campaign=x")
    detail_url = "https://amgen.wd1.myworkdayjobs.com/wday/cxs/amgen/Careers/job/NL/Role_R-200123"
    detail = {"jobPostingInfo": {"title": "Role", "jobDescription": "<p>Do things.</p>"}}
    store, calls = FakeSnapshotStore(), []
    with patch("jobtracker.ingest.requests.get", _fake_get({detail_url: (detail, "{}", None)}, calls)):
        job = ingest_url(url, snapshot_store=store)
    assert calls == [detail_url]
    assert job["job_id"] == "workday:amgen:Role_R-200123"
    assert "?" not in job["url"]
    print("PASS test_ingest_workday_detail_strips_tracking_query_string")


def test_ingest_recruitee_detail():
    url = "https://acme.recruitee.com/o/data-engineer"
    detail_url = "https://acme.recruitee.com/api/offers/data-engineer"
    detail = {"offer": {
        "title": "Data Engineer", "careers_url": url,
        "description": "<p>Build data infra.</p>", "guid": "abc123",
    }}
    store, calls = FakeSnapshotStore(), []
    with patch("jobtracker.ingest.requests.get", _fake_get({detail_url: (detail, "{}", None)}, calls)):
        job = ingest_url(url, snapshot_store=store)
    assert calls == [detail_url]
    assert job["title"] == "Data Engineer"
    assert store.saved[0]["platform"] == "recruitee"
    print("PASS test_ingest_recruitee_detail")


# --------------------------------------------------------------------------- #
# Known-platform fetch failure falls through to the generic path              #
# --------------------------------------------------------------------------- #
def test_ingest_falls_back_to_generic_when_known_platform_404s():
    # A SmartRecruiters "Apply" link matches the jobs.smartrecruiters.com/<a>/<b>
    # pattern but with the wrong path shape (oneclick-ui/company/<slug>/...) --
    # the resulting detail call 404s. That must fall through to the generic
    # trafilatura path instead of surfacing a raw request error.
    url = "https://jobs.smartrecruiters.com/oneclick-ui/company/Acme/publication/abc-123"
    bad_detail_url = "https://api.smartrecruiters.com/v1/companies/oneclick-ui/postings/company"
    html = (FIXTURES / "generic_vacancy.html").read_text(encoding="utf-8")
    store, calls = FakeSnapshotStore(), []
    responses = {
        bad_detail_url: (None, "", 404),
        url: (None, html, None),
    }
    with patch("jobtracker.ingest.requests.get", _fake_get(responses, calls)):
        job = ingest_url(url, snapshot_store=store)
    assert calls == [bad_detail_url, url]
    assert job["source"] == "Ingested"
    assert store.saved[0]["platform"] == "html"
    print("PASS test_ingest_falls_back_to_generic_when_known_platform_404s")


# --------------------------------------------------------------------------- #
# Generic fallback (trafilatura) + pasted text + thin-content                 #
# --------------------------------------------------------------------------- #
def test_ingest_generic_page_uses_trafilatura():
    url = "https://example-institute.org/careers/research-software-engineer"
    html = (FIXTURES / "generic_vacancy.html").read_text(encoding="utf-8")
    store = FakeSnapshotStore()
    with patch("jobtracker.ingest.requests.get", _fake_get({url: (None, html, None)})):
        job = ingest_url(url, snapshot_store=store)
    assert job["source"] == "Ingested"
    assert job["title"] == "Research Software Engineer"  # boilerplate site-name tail stripped
    assert job["company"] == "Example Institute"  # domain-derived fallback (no og:site_name)
    assert "genomic" in job["description"].lower()
    assert "cookie" not in job["description"].lower()  # chrome stripped by trafilatura
    assert store.saved[0]["platform"] == "html"
    assert store.saved[0]["raw_source"] == html
    print("PASS test_ingest_generic_page_uses_trafilatura")


def test_ingest_pasted_text_skips_network():
    store = FakeSnapshotStore()
    text = ("Senior Developer at Acme. "
           + ("Relevant responsibilities and requirements. " * 10).strip())
    job = ingest_url("https://example.com/vacancy/1", paste_text=text, snapshot_store=store)
    assert job["source"] == "Pasted"
    assert job["description"] == text
    assert store.saved[0]["platform"] == "paste"
    print("PASS test_ingest_pasted_text_skips_network")


def test_ingest_pasted_text_takes_precedence_over_url():
    # Pasted text is an explicit override: even a URL that WOULD resolve via a
    # known-platform adapter must not be fetched when text is also pasted.
    store, calls = FakeSnapshotStore(), []
    with patch("jobtracker.ingest.requests.get", _fake_get({}, calls)):
        job = ingest_url("https://jobs.lever.co/acme/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                         paste_text="Pasted override text.", snapshot_store=store)
    assert calls == []
    assert job["source"] == "Pasted"
    print("PASS test_ingest_pasted_text_takes_precedence_over_url")


def test_ingest_thin_content_raises_and_still_saves_snapshot():
    url = "https://spa-example.com/careers/1"
    html = "<html><body><div id='app'></div></body></html>"
    store = FakeSnapshotStore()
    with patch("jobtracker.ingest.requests.get", _fake_get({url: (None, html, None)})):
        try:
            ingest_url(url, snapshot_store=store)
        except ThinContentError as e:
            assert e.url == url
        else:
            raise AssertionError("expected ThinContentError")
    assert store.saved[0]["platform"] == "thin"
    assert store.saved[0]["raw_source"] == html
    print("PASS test_ingest_thin_content_raises_and_still_saves_snapshot")


def test_ingest_requires_url_or_paste():
    try:
        ingest_url("", snapshot_store=FakeSnapshotStore())
    except IngestInputError:
        pass
    else:
        raise AssertionError("expected IngestInputError")
    print("PASS test_ingest_requires_url_or_paste")


def test_ingest_pasted_text_fills_company_and_title_from_url():
    # Regression test: the paste path used to ship company="" and the raw
    # (uncleaned) first line as title, producing "Dear  team," and "...bij ."
    # in the generated letter/CV for every pasted vacancy.
    store = FakeSnapshotStore()
    job = ingest_url("https://werkenbij.acme.com/vacancy/1",
                     paste_text="Senior Developer - Careers | Acme\n\nFull vacancy text.",
                     snapshot_store=store)
    assert job["company"] == "Acme"
    assert job["title"] == "Senior Developer"
    print("PASS test_ingest_pasted_text_fills_company_and_title_from_url")


def test_ingest_pasted_text_without_url_has_no_company():
    # No URL at all (pure copy-paste, no "fetch failed" context) -> no domain
    # to derive a company from; empty is the honest answer, not a guess.
    store = FakeSnapshotStore()
    job = ingest_url("", paste_text="Some vacancy text here.", snapshot_store=store)
    assert job["company"] == ""
    print("PASS test_ingest_pasted_text_without_url_has_no_company")


# --------------------------------------------------------------------------- #
# Finding B: trafilatura's sitename falls back to a bare-domain form (and     #
# strips a leading "www."), which must not be mistaken for a real signal.     #
# --------------------------------------------------------------------------- #
def test_ingest_generic_page_www_prefix_no_sitename_uses_domain_fallback():
    url = "https://www.acme-labs.com/careers/data-engineer"
    html = ("<html><head><title>Data Engineer</title></head><body><p>"
           + ("Build data pipelines and own the platform. " * 15) + "</p></body></html>")
    store = FakeSnapshotStore()
    with patch("jobtracker.ingest.requests.get", _fake_get({url: (None, html, None)})):
        job = ingest_url(url, snapshot_store=store)
    # trafilatura's sitename fallback for this URL is "acme-labs.com" (www
    # stripped) -- without the bare-domain guard this would be used as-is.
    assert job["company"] == "Acme Labs"
    print("PASS test_ingest_generic_page_www_prefix_no_sitename_uses_domain_fallback")


def test_ingest_generic_page_with_real_sitename_is_kept():
    url = "https://www.acme-labs.com/careers/data-engineer"
    html = ("<html><head><title>Data Engineer</title>"
           "<meta property=\"og:site_name\" content=\"Acme Labs Careers\"></head>"
           "<body><p>" + ("Build data pipelines and own the platform. " * 15)
           + "</p></body></html>")
    store = FakeSnapshotStore()
    with patch("jobtracker.ingest.requests.get", _fake_get({url: (None, html, None)})):
        job = ingest_url(url, snapshot_store=store)
    assert job["company"] == "Acme Labs Careers"
    print("PASS test_ingest_generic_page_with_real_sitename_is_kept")


# --------------------------------------------------------------------------- #
# Finding D: a thin-content snapshot's raw HTML must survive a later paste    #
# save for the SAME url (the documented recovery path), not get blanked.      #
# --------------------------------------------------------------------------- #
def test_thin_snapshot_survives_later_paste_save():
    tmp = Path(tempfile.mkdtemp(prefix="jt_snap_")) / "vacancy_snapshots.json"
    store = LocalJSONSnapshotStore(str(tmp))
    url = "https://spa-example.com/careers/1"
    store.save(url, job_id="", platform="thin", raw_source="<html>shell</html>",
              extracted_text="")
    store.save(url, job_id="ingest:abc", platform="paste", raw_source="",
              extracted_text="pasted text")
    row = store.get(url)
    assert row["raw_source"] == "<html>shell</html>"  # not wiped by the empty paste save
    assert row["extracted_text"] == "pasted text"      # the new non-empty value DID apply
    assert row["platform"] == "paste"                  # non-empty fields still update normally
    print("PASS test_thin_snapshot_survives_later_paste_save")


# --------------------------------------------------------------------------- #
# Findings G, H: direct unit coverage for the two heuristics                  #
# --------------------------------------------------------------------------- #
def test_company_from_domain_handles_second_level_suffixes():
    assert _company_from_domain("https://www.jobs.ox.ac.uk/vacancy/1") == "Ox"
    assert _company_from_domain("https://careers.acme.co.uk/role/1") == "Acme"
    assert _company_from_domain("https://acme.io/careers/1") == "Acme"
    print("PASS test_company_from_domain_handles_second_level_suffixes")


def test_clean_title_keeps_legitimate_dash_titles():
    # A dash/em-dash tail that is neither boilerplate nor the company name
    # must be kept -- these are real, especially in Dutch postings.
    assert _clean_title("Data Engineer - Clinical Genomics", "Acme") == \
        "Data Engineer - Clinical Genomics"
    assert _clean_title("Senior Onderzoeker — afdeling Genomics", "UMC Utrecht") == \
        "Senior Onderzoeker — afdeling Genomics"
    print("PASS test_clean_title_keeps_legitimate_dash_titles")


def test_clean_title_strips_boilerplate_or_company_tail():
    assert _clean_title("Data Engineer - Careers | Acme", "Acme") == "Data Engineer"
    assert _clean_title("Role Title | Acme Careers", "Acme") == "Role Title"
    assert _clean_title("Backend Developer - Werken bij Acme", "Acme") == "Backend Developer"
    print("PASS test_clean_title_strips_boilerplate_or_company_tail")


# --------------------------------------------------------------------------- #
# snapshot_id: host is normalized, but path case is kept significant          #
# --------------------------------------------------------------------------- #
def test_snapshot_id_normalizes_host_case_only():
    assert snapshot_id("https://Example.COM/careers/Role") == \
        snapshot_id("https://example.com/careers/Role")
    assert snapshot_id("https://example.com/careers/Role") != \
        snapshot_id("https://example.com/careers/role")
    print("PASS test_snapshot_id_normalizes_host_case_only")


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
    test_ingest_workday_detail_strips_tracking_query_string()
    test_ingest_recruitee_detail()
    test_ingest_falls_back_to_generic_when_known_platform_404s()
    test_ingest_generic_page_uses_trafilatura()
    test_ingest_pasted_text_skips_network()
    test_ingest_pasted_text_takes_precedence_over_url()
    test_ingest_thin_content_raises_and_still_saves_snapshot()
    test_ingest_requires_url_or_paste()
    test_ingest_pasted_text_fills_company_and_title_from_url()
    test_ingest_pasted_text_without_url_has_no_company()
    test_ingest_generic_page_www_prefix_no_sitename_uses_domain_fallback()
    test_ingest_generic_page_with_real_sitename_is_kept()
    test_thin_snapshot_survives_later_paste_save()
    test_company_from_domain_handles_second_level_suffixes()
    test_clean_title_keeps_legitimate_dash_titles()
    test_clean_title_strips_boilerplate_or_company_tail()
    test_snapshot_id_normalizes_host_case_only()
    test_snapshot_survives_posting_removed()
    print("\nAll ingest tests passed.")
