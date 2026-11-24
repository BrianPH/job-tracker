"""Offline tests for the greenhouse, lever, remotive, remoteok, academictransfer
and html adapters (parsers only, except the fetch_html title-filter tests, which
monkeypatch requests.get with an in-memory fake -- no real network)."""
from unittest.mock import MagicMock, patch

from jobtracker.adapters import (
    extract_html_links,
    fetch_html,
    parse_adzuna_job,
    parse_jooble_job,
    parse_academictransfer_result,
    parse_greenhouse_job,
    parse_lever_posting,
    parse_remoteok_job,
    parse_remotive_job,
    _title_matches_any,
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


def test_extract_html_links_generic_cta_uses_title_attr():
    html = """
    <html><body>
      <div class="card">
        <h3>QA/RA Lead</h3>
        <a href="/en/vacancies/qa-ra-lead/" title="QA/RA Lead">Read more</a>
      </div>
      <a href="/en/vacancies/legal-counsel/" title="Legal Counsel">Read more</a>
      <a href="/en/vacancies/feed/">Feed</a>
    </body></html>"""
    links = extract_html_links(html, "https://example.com/en/vacancies/",
                                link_pattern="/en/vacancies/(?!feed)[a-z0-9-]+/?$")
    by_url = {url: title for title, url in links}
    assert by_url["https://example.com/en/vacancies/qa-ra-lead/"] == "QA/RA Lead"
    assert by_url["https://example.com/en/vacancies/legal-counsel/"] == "Legal Counsel"
    assert not any("feed" in u for u in by_url)
    print("PASS test_extract_html_links_generic_cta_uses_title_attr")


def test_extract_html_links_generic_cta_dutch():
    html = """
    <html><body>
      <a href="/vacatures/data-engineer" title="Data Engineer">Lees meer</a>
      <a href="/vacatures/qa-lead" title="QA Lead">Solliciteer nu</a>
    </body></html>"""
    links = extract_html_links(html, "https://example.nl/vacatures", link_pattern="/vacatures/[a-z-]")
    by_url = {url: title for title, url in links}
    assert by_url["https://example.nl/vacatures/data-engineer"] == "Data Engineer"
    assert by_url["https://example.nl/vacatures/qa-lead"] == "QA Lead"
    print("PASS test_extract_html_links_generic_cta_dutch")


_LISTING_HTML = """
<html><body>
  <a href="/vacatures/software-engineer">Software Engineer</a>
  <a href="/vacatures/data-scientist">Data Scientist</a>
  <a href="/vacatures/verpleegkundige-cardiologie">Verpleegkundige Cardiologie</a>
  <a href="/vacatures/phd-candidate-genomics">PhD Candidate Genomics</a>
</body></html>
"""


def _fake_html_get(detail_calls):
    def _fake(url, headers=None, timeout=None):
        resp = MagicMock()
        resp.raise_for_status = lambda: None
        if url.endswith("/vacatures"):
            resp.text = _LISTING_HTML
        else:
            detail_calls.append(url)
            resp.text = "<html><body><p>Full job description text.</p></body></html>"
        return resp
    return _fake


def test_fetch_html_title_include_skips_detail_fetch_for_non_matching():
    detail_calls = []
    with patch("jobtracker.adapters.requests.get", _fake_html_get(detail_calls)):
        jobs = fetch_html("TestOrg", {
            "careers_url": "https://example.nl/vacatures",
            "link_pattern": "/vacatures/[a-z-]+$",
            "title_include": ["software", "data", "engineer", "scientist"],
        })
    titles = [j.title for j in jobs]
    assert "Software Engineer" in titles and "Data Scientist" in titles
    assert "Verpleegkundige Cardiologie" not in titles and "PhD Candidate Genomics" not in titles
    # the two non-matching links must never have triggered a detail request
    assert len(detail_calls) == 2
    print("PASS test_fetch_html_title_include_skips_detail_fetch_for_non_matching")


def test_fetch_html_title_exclude_drops_matching_titles():
    detail_calls = []
    with patch("jobtracker.adapters.requests.get", _fake_html_get(detail_calls)):
        jobs = fetch_html("TestOrg", {
            "careers_url": "https://example.nl/vacatures",
            "link_pattern": "/vacatures/[a-z-]+$",
            "title_exclude": ["verpleegkundige", "phd"],
        })
    titles = [j.title for j in jobs]
    assert "Software Engineer" in titles and "Data Scientist" in titles
    assert "Verpleegkundige Cardiologie" not in titles and "PhD Candidate Genomics" not in titles
    assert len(detail_calls) == 2
    print("PASS test_fetch_html_title_exclude_drops_matching_titles")


def test_fetch_html_without_title_filters_keeps_everything():
    detail_calls = []
    with patch("jobtracker.adapters.requests.get", _fake_html_get(detail_calls)):
        jobs = fetch_html("TestOrg", {
            "careers_url": "https://example.nl/vacatures",
            "link_pattern": "/vacatures/[a-z-]+$",
        })
    assert len(jobs) == 4
    assert len(detail_calls) == 4
    print("PASS test_fetch_html_without_title_filters_keeps_everything")


def test_fetch_html_title_exclude_wins_over_title_include():
    # A title matching BOTH an include and an exclude keyword must be
    # dropped -- exclude always wins, regardless of list order in params.
    html = """
    <html><body>
      <a href="/vacatures/software-engineer">Software Engineer</a>
      <a href="/vacatures/phd-software-engineer">PhD Candidate Software Engineer</a>
    </body></html>
    """
    detail_calls = []

    def fake_get(url, headers=None, timeout=None):
        resp = MagicMock()
        resp.raise_for_status = lambda: None
        if url.endswith("/vacatures"):
            resp.text = html
        else:
            detail_calls.append(url)
            resp.text = "<html><body><p>Full job description text.</p></body></html>"
        return resp

    with patch("jobtracker.adapters.requests.get", fake_get):
        jobs = fetch_html("TestOrg", {
            "careers_url": "https://example.nl/vacatures",
            "link_pattern": "/vacatures/[a-z-]+$",
            "title_include": ["software", "engineer"],
            "title_exclude": ["phd"],
        })
    titles = [j.title for j in jobs]
    assert titles == ["Software Engineer"]
    assert len(detail_calls) == 1
    print("PASS test_fetch_html_title_exclude_wins_over_title_include")


def test_fetch_html_max_results_applies_after_title_filtering():
    # max_results must cap the FILTERED set, not the raw listing -- a link
    # dropped by title_include/title_exclude must never consume the budget
    # meant for relevant results.
    html = """
    <html><body>
      <a href="/vacatures/software-engineer">Software Engineer</a>
      <a href="/vacatures/data-engineer">Data Engineer</a>
      <a href="/vacatures/devops-engineer">DevOps Engineer</a>
      <a href="/vacatures/verpleegkundige">Verpleegkundige</a>
      <a href="/vacatures/phd-candidate">PhD Candidate</a>
    </body></html>
    """
    detail_calls = []

    def fake_get(url, headers=None, timeout=None):
        resp = MagicMock()
        resp.raise_for_status = lambda: None
        if url.endswith("/vacatures"):
            resp.text = html
        else:
            detail_calls.append(url)
            resp.text = "<html><body><p>Full job description text.</p></body></html>"
        return resp

    with patch("jobtracker.adapters.requests.get", fake_get):
        jobs = fetch_html("TestOrg", {
            "careers_url": "https://example.nl/vacatures",
            "link_pattern": "/vacatures/[a-z-]+$",
            "title_include": ["engineer"],
            "max_results": 2,
        })
    # 3 links match "engineer"; max_results=2 must cap that filtered set to 2,
    # not the 5 raw links, and must never detail-fetch the 2 non-matching ones.
    assert len(jobs) == 2
    assert len(detail_calls) == 2
    assert all("Verpleegkundige" not in u and "phd-candidate" not in u for u in detail_calls)
    print("PASS test_fetch_html_max_results_applies_after_title_filtering")


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


def test_exclude_keyword_does_not_match_inside_a_longer_word():
    # "arts" (Dutch for "physician") is a real decided exclude keyword, but
    # plain substring matching would also drop any job whose text happens to
    # contain "starts", "parts", "charts", etc.
    rules = {"exclude_any": ["arts"]}
    physician = Job(title="Arts", company="A", source="t",
                     description="Je werkt als arts op de afdeling cardiologie.")
    unrelated = Job(title="Software Engineer", company="A", source="t",
                     description="The position starts in January; build dashboards and charts.")
    assert not passes(physician, rules)
    assert passes(unrelated, rules)
    print("PASS test_exclude_keyword_does_not_match_inside_a_longer_word")


def test_exclude_keyword_matches_dutch_plural_inflection():
    # The exclude keyword is the Dutch singular; the actual job title is
    # almost always the plural ("-n"/"-en" suffix). A left-anchored keyword
    # must still match the inflected form, not just the bare singular.
    rules = {"exclude_any": ["verpleegkundige", "laborant"]}
    assert not passes(Job(title="Verpleegkundigen IC", company="A", source="t"), rules)
    assert not passes(Job(title="Laboranten Pathologie", company="A", source="t"), rules)
    print("PASS test_exclude_keyword_matches_dutch_plural_inflection")


def test_exclude_tail_compound_needs_its_own_keyword():
    # "arts" alone can't exclude a tail compound like "huisarts" without also
    # breaking it as a standalone word / left-anchored prefix elsewhere --
    # that's why profile.example.yaml lists "huisarts"/"tandarts" separately.
    generic_only = {"exclude_any": ["arts"]}
    with_compound = {"exclude_any": ["arts", "huisarts"]}
    huisarts = Job(title="Huisarts", company="A", source="t")
    assert passes(huisarts, generic_only)       # the residual gap
    assert not passes(huisarts, with_compound)  # closed by the explicit keyword
    print("PASS test_exclude_tail_compound_needs_its_own_keyword")


def test_title_include_matches_truncated_prefix():
    # "bioinformatic" is a deliberately truncated include keyword meant to
    # match several inflections/spellings at once.
    keywords = ["bioinformatic", "engineer"]
    for title in ["Bioinformatician", "Bioinformatics Specialist",
                  "Bioinformatica Analist", "Senior Engineering Manager"]:
        assert _title_matches_any(title, keywords), title
    assert not _title_matches_any("Verpleegkundige IC", keywords)
    print("PASS test_title_include_matches_truncated_prefix")


if __name__ == "__main__":
    test_parse_greenhouse()
    test_parse_lever()
    test_parse_remotive()
    test_parse_remoteok()
    test_parse_academictransfer()
    test_extract_html_links()
    test_extract_html_links_generic_cta_uses_title_attr()
    test_extract_html_links_generic_cta_dutch()
    test_fetch_html_title_include_skips_detail_fetch_for_non_matching()
    test_fetch_html_title_exclude_drops_matching_titles()
    test_fetch_html_without_title_filters_keeps_everything()
    test_fetch_html_title_exclude_wins_over_title_include()
    test_fetch_html_max_results_applies_after_title_filtering()
    test_parse_adzuna()
    test_parse_jooble()
    test_salary_setpoint()
    test_exclude_keyword_does_not_match_inside_a_longer_word()
    test_exclude_keyword_matches_dutch_plural_inflection()
    test_exclude_tail_compound_needs_its_own_keyword()
    test_title_include_matches_truncated_prefix()
    print("\nAll new-adapter tests passed.")
