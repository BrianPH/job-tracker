"""Offline tests for the greenhouse, lever, remotive, remoteok, academictransfer
and html adapters (parsers only — no network)."""
from jobtracker.adapters import (
    extract_html_links,
    parse_adzuna_job,
    parse_jooble_job,
    parse_academictransfer_result,
    parse_greenhouse_job,
    parse_lever_posting,
    parse_remoteok_job,
    parse_remotive_job,
)
from jobtracker.filters import parse_salary_max, passes
from jobtracker.models import Job


def test_parse_greenhouse():
    item = {
        "id": 12345,
        "title": "Backend Engineer",
        "absolute_url": "https://boards.greenhouse.io/acme/jobs/12345",
        "location": {"name": "Amsterdam, Netherlands"},
        "updated_at": "2026-07-01T10:00:00-04:00",
        "content": "&lt;p&gt;Build &amp;amp; scale Python services.&lt;/p&gt;",
    }
    job = parse_greenhouse_job(item, "Acme")
    assert job.job_id == "greenhouse:12345"
    assert job.title == "Backend Engineer"
    assert job.location == "Amsterdam, Netherlands"
    assert job.posted_date == "2026-07-01"
    assert "Build & scale Python services." in job.description
    assert not job.remote
    remote_item = dict(item, location={"name": "Remote - EMEA"})
    assert parse_greenhouse_job(remote_item, "Acme").remote
    print("PASS test_parse_greenhouse")


def test_parse_lever():
    item = {
        "id": "abc-123",
        "text": "Software Engineer, Platform",
        "hostedUrl": "https://jobs.lever.co/acme/abc-123",
        "categories": {"location": "Remote (Europe)", "team": "Engineering"},
        "workplaceType": "remote",
        "createdAt": 1751328000000,
        "descriptionPlain": "Design and build backend systems in Python.",
        "salaryRange": {"min": 60000, "max": 80000, "currency": "EUR", "interval": "per-year-salary"},
    }
    job = parse_lever_posting(item, "Acme")
    assert job.job_id == "lever:abc-123"
    assert job.remote
    assert "60000-80000 EUR" in job.salary
    assert job.posted_date.startswith("2025") or job.posted_date.startswith("2026")
    assert "backend systems" in job.description
    print("PASS test_parse_lever")


def test_parse_remotive():
    item = {
        "id": 999,
        "url": "https://remotive.com/remote-jobs/software-dev/dev-999",
        "title": "Python Developer",
        "company_name": "RemoteCo",
        "candidate_required_location": "Worldwide",
        "salary": "$90k - $120k",
        "publication_date": "2026-07-01T08:00:00",
        "description": "<p>Develop APIs with Python and PostgreSQL.</p>",
    }
    job = parse_remotive_job(item, "Remotive")
    assert job.job_id == "remotive:999"
    assert job.remote and job.company == "RemoteCo"
    assert job.posted_date == "2026-07-01"
    assert "Develop APIs" in job.description
    print("PASS test_parse_remotive")


def test_parse_remoteok():
    item = {
        "id": "777", "slug": "backend-dev-777",
        "position": "Backend Developer",
        "company": "OKCo",
        "location": "Worldwide",
        "tags": ["python", "backend"],
        "date": "2026-07-02T00:00:00+00:00",
        "salary_min": 70000, "salary_max": 100000,
        "url": "https://remoteok.com/remote-jobs/777",
        "description": "Build things.",
    }
    job = parse_remoteok_job(item)
    assert job.job_id == "remoteok:777"
    assert job.remote and job.salary == "70000-100000 USD"
    assert job.posted_date == "2026-07-02"
    print("PASS test_parse_remoteok")


def test_parse_academictransfer():
    item = {
        "id": 555,
        "title": "Bioinformatician",
        "employer_name": "LUMC",
        "city": "Leiden",
        "url": "/nl/jobs/555/bioinformatician/",
        "publish_date": "2026-06-20",
        "abstract": "Analyse NGS data for the genetics department.",
    }
    job = parse_academictransfer_result(item, "Leiden University Medical Center")
    assert job.job_id == "academictransfer:555"
    assert job.url.startswith("https://www.academictransfer.com/")
    assert job.company == "LUMC" and job.location == "Leiden"
    assert "NGS data" in job.description
    print("PASS test_parse_academictransfer")


def test_extract_html_links():
    html = """
    <html><body>
      <a href="/vacancies/bioinformatician">Bioinformatician</a>
      <a href="/about">About us</a>
      <a href="/vacancies/software-engineer">Software Engineer</a>
      <a href="mailto:info@x.nl">Mail</a>
      <a href="/vacancies/bioinformatician">Bioinformatician (duplicate)</a>
    </body></html>"""
    links = extract_html_links(html, "https://example.com/careers", link_pattern="/vacancies/.")
    urls = [u for _, u in links]
    assert "https://example.com/vacancies/bioinformatician" in urls
    assert "https://example.com/vacancies/software-engineer" in urls
    assert all("/about" not in u and "mailto" not in u for u in urls)
    assert len([u for u in urls if u.endswith("/bioinformatician")]) == 1  # dedup
    print("PASS test_extract_html_links (pattern + dedup)")


def test_parse_adzuna():
    item = {
        "id": "4321",
        "title": "Junior <strong>Software</strong> Developer",
        "description": "Build web applications in C# and Python...",
        "redirect_url": "https://www.adzuna.nl/land/ad/4321",
        "company": {"display_name": "TechBV"},
        "location": {"display_name": "Rotterdam, Zuid-Holland"},
        "salary_min": 38000.0, "salary_max": 48000.0,
        "created": "2026-07-01T12:00:00Z",
    }
    job = parse_adzuna_job(item)
    assert job.job_id == "adzuna:4321"
    assert job.title == "Junior Software Developer"       # <strong> stripped
    assert job.company == "TechBV" and "Rotterdam" in job.location
    assert job.salary == "38000-48000 EUR"
    assert job.posted_date == "2026-07-01"
    print("PASS test_parse_adzuna")


def test_parse_jooble():
    item = {
        "id": 987654,
        "title": "Bioinformatician (Remote)",
        "location": "Netherlands",
        "snippet": "Analyse <b>NGS</b> data and build pipelines in Python.",
        "salary": "", "source": "example.nl", "type": "Full-time",
        "link": "https://jooble.org/desc/987654",
        "company": "BioTech NL",
        "updated": "2026-07-05T00:00:00.0000000",
    }
    job = parse_jooble_job(item)
    assert job.job_id == "jooble:987654"
    assert job.remote                                     # "Remote" in title
    assert job.company == "BioTech NL"
    assert "NGS" in job.description and "<b>" not in job.description
    assert job.posted_date == "2026-07-05"
    print("PASS test_parse_jooble")


def test_salary_setpoint():
    assert parse_salary_max("50000-60000 EUR") == 60000
    assert parse_salary_max("€3.500 - €4.500 per month") == 54000
    assert parse_salary_max("90k-120k USD") == 120000
    assert parse_salary_max("") is None
    assert parse_salary_max("competitive") is None

    rules = {"keywords_any": ["python"], "min_salary": 55000}
    ok = Job(title="Python dev", company="A", source="t", salary="60000 EUR", location="Leiden")
    low = Job(title="Python dev", company="A", source="t", salary="30000-40000 EUR", location="Leiden")
    none = Job(title="Python dev", company="A", source="t", salary="", location="Leiden")
    assert passes(ok, rules)
    assert not passes(low, rules)
    assert passes(none, rules)  # unstated salary never drops a job
    print("PASS test_salary_setpoint")


if __name__ == "__main__":
    test_parse_greenhouse()
    test_parse_lever()
    test_parse_remotive()
    test_parse_remoteok()
    test_parse_academictransfer()
    test_extract_html_links()
    test_parse_adzuna()
    test_parse_jooble()
    test_salary_setpoint()
    print("\nAll new-adapter tests passed.")
