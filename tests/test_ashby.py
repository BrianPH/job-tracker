"""Offline tests for the ashby adapter (parser only — no network)."""
from jobtracker.adapters import parse_ashby_job


def test_parse_ashby_basic():
    item = {
        "id": "d056f390-1d70-408e-8f85-2360d41eaa84",
        "title": "Business Support Specialist - Dutch",
        "location": "Maastricht",
        "secondaryLocations": [],
        "publishedAt": "2026-06-24T07:40:12.278+00:00",
        "isListed": True,
        "isRemote": None,
        "workplaceType": None,
        "jobUrl": "https://jobs.ashbyhq.com/mollie/d056f390-1d70-408e-8f85-2360d41eaa84",
        "applyUrl": "https://jobs.ashbyhq.com/mollie/d056f390-1d70-408e-8f85-2360d41eaa84/application",
        "descriptionHtml": "<p>Build with us.</p>",
    }
    job = parse_ashby_job(item, "Mollie")
    assert job.job_id == "ashby:d056f390-1d70-408e-8f85-2360d41eaa84"
    assert job.title == "Business Support Specialist - Dutch"
    assert job.location == "Maastricht"
    assert not job.remote
    assert job.url == "https://jobs.ashbyhq.com/mollie/d056f390-1d70-408e-8f85-2360d41eaa84"
    assert job.posted_date == "2026-06-24"
    assert "Build with us" in job.description
    print("PASS test_parse_ashby_basic")


def test_parse_ashby_remote_and_secondary_locations():
    item = {
        "id": "abc",
        "title": "Support Engineer",
        "location": "Amsterdam",
        "secondaryLocations": [{"location": "Berlin"}, {"location": "Remote - EU"}],
        "isRemote": True,
        "workplaceType": "Remote",
        "jobUrl": "https://jobs.ashbyhq.com/zapier/abc",
    }
    job = parse_ashby_job(item, "Zapier")
    assert job.remote
    assert job.location == "Amsterdam, Berlin, Remote - EU"
    print("PASS test_parse_ashby_remote_and_secondary_locations")


def test_parse_ashby_unlisted_filtered_by_fetch():
    # isListed defaults to True when absent; fetch_ashby filters on this field,
    # parse_ashby_job itself does not — verify the parser still builds a Job either way.
    item = {"id": "x", "title": "Draft role", "isListed": False}
    job = parse_ashby_job(item, "Acme")
    assert job.job_id == "ashby:x"
    print("PASS test_parse_ashby_unlisted_filtered_by_fetch")


if __name__ == "__main__":
    test_parse_ashby_basic()
    test_parse_ashby_remote_and_secondary_locations()
    test_parse_ashby_unlisted_filtered_by_fetch()
    print("\nAll ashby adapter tests passed.")
