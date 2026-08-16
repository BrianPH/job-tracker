"""Offline tests for the smartrecruiters adapter (parser only — no network)."""
from jobtracker.adapters import apply_smartrecruiters_detail, parse_smartrecruiters_posting
from jobtracker.models import Job


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


if __name__ == "__main__":
    test_parse_smartrecruiters_posting()
    test_apply_smartrecruiters_detail()
    test_apply_smartrecruiters_detail_empty_is_noop()
    print("\nAll smartrecruiters adapter tests passed.")
