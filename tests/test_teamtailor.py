"""Offline tests for the Teamtailor adapter (parses JSON Feed items). No network.
Run:  python -m tests.test_teamtailor
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jobtracker.adapters import parse_teamtailor_item

# Shaped like a Teamtailor /jobs.json (JSON Feed 1.1) item.
ITEM = {
    "id": "https://careers.huborganoids.nl/jobs/1234567-bioinformatics-scientist",
    "url": "https://careers.huborganoids.nl/jobs/1234567-bioinformatics-scientist",
    "title": "Bioinformatics Scientist",
    "content_text": "We are looking for a bioinformatics scientist to build organoid data pipelines.",
    "content_html": "<p>We are looking for a <b>bioinformatics</b> scientist.</p>",
    "date_published": "2026-06-15T09:00:00Z",
    "tags": ["Utrecht", "R&D"],
}


def test_parse_item():
    j = parse_teamtailor_item(ITEM, "HUB Organoids")
    assert j.title == "Bioinformatics Scientist"
    assert j.company == "HUB Organoids"
    assert j.source == "Teamtailor"
    assert j.url.endswith("bioinformatics-scientist")
    assert j.job_id == "teamtailor:1234567-bioinformatics-scientist"
    assert j.location == "Utrecht, R&D"
    assert j.posted_date == "2026-06-15"
    assert "bioinformatics" in j.description.lower()
    print("PASS test_parse_item")


def test_html_fallback_and_remote():
    item = {k: v for k, v in ITEM.items() if k != "content_text"}
    item["title"] = "Remote Data Engineer"
    j = parse_teamtailor_item(item, "HUB Organoids")
    assert "<p>" not in j.description  # html stripped
    assert j.remote is True           # 'remote' detected in title
    print("PASS test_html_fallback_and_remote")


if __name__ == "__main__":
    test_parse_item()
    test_html_fallback_and_remote()
    print("\nAll Teamtailor tests passed.")
