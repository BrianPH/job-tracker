"""Offline tests for the Workday adapter parsers (no network).

Uses embedded sample payloads shaped like Workday's CXS list + detail responses.
Run:  python -m tests.test_workday
"""
import datetime
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jobtracker.adapters import (
    apply_workday_detail,
    parse_workday_posted,
    parse_workday_posting,
)

HOST = "amgen.wd1.myworkdayjobs.com"
SITE = "Careers"
TENANT = "amgen"

LIST_POSTING = {
    "title": "Scientist, Bioinformatics",
    "externalPath": "/job/Netherlands-Breda/Scientist--Bioinformatics_R-200123",
    "locationsText": "Netherlands - Breda",
    "postedOn": "Posted 5 Days Ago",
    "bulletFields": ["R-200123"],
}

DETAIL_INFO = {
    "title": "Scientist, Bioinformatics",
    "jobDescription": "<p>We seek a <b>bioinformatics</b> scientist to build NGS pipelines.</p>"
                      "<ul><li>Python</li><li>Genomics</li></ul>",
    "location": "Netherlands - Breda",
    "postedOn": "Posted 5 Days Ago",
    "startDate": "2026-06-20",
    "remoteType": "Hybrid",
}


def test_parse_posted():
    today = datetime.date(2026, 7, 7)
    assert parse_workday_posted("Posted Today", today) == "2026-07-07"
    assert parse_workday_posted("Posted Yesterday", today) == "2026-07-06"
    assert parse_workday_posted("Posted 5 Days Ago", today) == "2026-07-02"
    assert parse_workday_posted("Posted 30+ Days Ago", today) == "2026-06-07"
    assert parse_workday_posted("Posted 2 Months Ago", today) == "2026-05-08"
    assert parse_workday_posted("", today) == ""
    assert parse_workday_posted(None, today) == ""
    print("PASS test_parse_posted")


def test_parse_posting():
    j = parse_workday_posting(LIST_POSTING, "Amgen Europe", HOST, SITE, TENANT)
    assert j.title == "Scientist, Bioinformatics"
    assert j.company == "Amgen Europe"
    assert j.source == "Workday"
    assert j.location == "Netherlands - Breda"
    assert j.url == ("https://amgen.wd1.myworkdayjobs.com/Careers"
                     "/job/Netherlands-Breda/Scientist--Bioinformatics_R-200123")
    assert j.job_id == "workday:amgen:Scientist--Bioinformatics_R-200123"
    assert j.posted_date != ""  # relative date parsed
    print("PASS test_parse_posting")


def test_apply_detail():
    j = parse_workday_posting(LIST_POSTING, "Amgen Europe", HOST, SITE, TENANT)
    apply_workday_detail(j, DETAIL_INFO)
    assert "<p>" not in j.description and "bioinformatics" in j.description
    assert "Python" in j.description
    assert j.remote is False  # Hybrid -> not remote
    # remoteType 'Remote' would flip it
    apply_workday_detail(j, {"remoteType": "Remote - Netherlands"})
    assert j.remote is True
    print("PASS test_apply_detail")


def test_detail_startdate_fallback():
    j = parse_workday_posting({**LIST_POSTING, "postedOn": ""}, "Amgen", HOST, SITE, TENANT)
    assert j.posted_date == ""
    apply_workday_detail(j, DETAIL_INFO)
    assert j.posted_date == "2026-06-20"  # fell back to startDate
    print("PASS test_detail_startdate_fallback")


if __name__ == "__main__":
    test_parse_posted()
    test_parse_posting()
    test_apply_detail()
    test_detail_startdate_fallback()
    print("\nAll Workday tests passed.")
