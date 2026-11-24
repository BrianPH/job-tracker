"""Offline tests for the werkenvoornederland adapter (RIVM, NFI, ...).
No network: requests.get is monkeypatched with saved HTML snippets shaped
like the site's real Hippo/BloomReach component-rendering responses.
Run:  python -m tests.test_werkenvoornederland
"""
import sys
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bs4 import BeautifulSoup

import jobtracker.adapters as adapters_mod
from jobtracker.adapters import fetch_werkenvoornederland, parse_werkenvoornederland_card


def _card_html(href: str, title: str, employer: str, location: str, salary: str) -> str:
    return f"""
    <li class="vacancy-list__item--positioning vacancy-list__item" data-govan-component="search-result-item">
    <section class="vacancy" data-baanpleinid="01760-20261124-0001">
    <h2 class="vacancy__title"><a data-govan-action="view-card" href="{href}">{title}</a></h2>
    <p class="vacancy__employer">{employer}</p>
    <div><div class="job-short-info--flex"><ul class="job-short-info--width">
      <li class="job-short-info__item job-short-info__item-icon">
        <span role="img" title="Locatie" aria-label="Locatie"></span>
        <span class="job-short-info__value job-short-info__value-icon">{location}</span>
      </li>
      <li class="job-short-info__item job-short-info__item-icon">
        <span role="img" title="Salaris" aria-label="Salaris"></span>
        <span class="job-short-info__value job-short-info__value-icon">{salary}</span>
      </li>
    </ul></div></div>
    </section>
    </li>
    """


def _results_page(n_cards: int, start: int = 0) -> str:
    if n_cards == 0:
        return '<span id="lastPage"></span>'  # what a page past the last one actually returns
    cards = "".join(
        _card_html(f"/vacatures/role-{start + i}-RIVM-2026-{start + i}",
                   f"Role {start + i}", "RIVM", "Bilthoven", "Schaal 11")
        for i in range(n_cards)
    )
    return f"""
    <div id="vacancy-results-container">
    <div class="vacancy-result-bar">
    <span class="vacancy-result-bar__totals-badge">{n_cards}</span>
    </div>
    <ul id="vacancies-list">{cards}</ul>
    </div>
    """


DETAIL_HTML = """
<html><body>
<div class="s-article-content"><p>Dit ga je doen: duties text.</p></div>
<div class="s-article-content"><p>Dit vragen wij: requirements text.</p></div>
<div class="s-article-content"><p>Over de organisatie: employer blurb.</p></div>
</body></html>
"""


class _FakeResponse:
    def __init__(self, text: str):
        self.text = text

    def raise_for_status(self):
        pass


@contextmanager
def _fake_get(handler):
    original = adapters_mod.requests.get

    def _fake(url, headers=None, timeout=None):
        return _FakeResponse(handler(url))

    adapters_mod.requests.get = _fake
    try:
        yield
    finally:
        adapters_mod.requests.get = original


def test_parse_werkenvoornederland_card():
    html = _card_html("/vacatures/projectleider-RIVM-2026-5056", "Projectleider veiligheid",
                       "RIVM", "Bilthoven", "Schaal 11 €4.132 - €6.275 (bruto)")
    section = BeautifulSoup(html, "html.parser").find("section", class_="vacancy")
    job = parse_werkenvoornederland_card(section, "RIVM")
    assert job.title == "Projectleider veiligheid"
    assert job.company == "RIVM"
    assert job.source == "WerkenVoorNederland"
    assert job.url == "https://www.werkenvoornederland.nl/vacatures/projectleider-RIVM-2026-5056"
    assert job.location == "Bilthoven"
    assert "6.275" in job.salary
    assert job.job_id.startswith("wvn:")
    print("PASS test_parse_werkenvoornederland_card")


def test_fetch_werkenvoornederland_no_werkgever_returns_empty():
    assert fetch_werkenvoornederland("RIVM", {}) == []
    print("PASS test_fetch_werkenvoornederland_no_werkgever_returns_empty")


def test_fetch_werkenvoornederland_passes_werkgever_param():
    seen_params = []

    def handler(url):
        q = parse_qs(urlparse(url).query)
        seen_params.append(q.get("werkgever", [None])[0])
        return _results_page(3)

    with _fake_get(handler):
        jobs = fetch_werkenvoornederland("RIVM", {"werkgever": "01760", "descriptions": False})
    assert len(jobs) == 3
    assert all(p == "01760" for p in seen_params)
    print("PASS test_fetch_werkenvoornederland_passes_werkgever_param")


def test_fetch_werkenvoornederland_stops_on_short_page():
    # A page with fewer than 20 cards is the last one -- no further page fetched.
    calls = []

    def handler(url):
        calls.append(url)
        return _results_page(5)  # < 20 -> last page

    with _fake_get(handler):
        jobs = fetch_werkenvoornederland("RIVM", {"werkgever": "01760", "descriptions": False,
                                                   "max_results": 50})
    assert len(jobs) == 5
    assert len(calls) == 1
    print("PASS test_fetch_werkenvoornederland_stops_on_short_page")


def test_fetch_werkenvoornederland_paginates_on_full_page():
    calls = []

    def handler(url):
        calls.append(url)
        if "pagina=2" in url:
            return _results_page(4, start=20)
        return _results_page(20, start=0)  # full page -> expect a page 2 fetch

    with _fake_get(handler):
        jobs = fetch_werkenvoornederland("RIVM", {"werkgever": "01760", "descriptions": False,
                                                   "max_results": 50})
    assert len(jobs) == 24
    assert len(calls) == 2
    assert "pagina=2" in calls[1] and "append-next-page-results" in calls[1]
    print("PASS test_fetch_werkenvoornederland_paginates_on_full_page")


def test_fetch_werkenvoornederland_respects_max_results():
    def handler(url):
        return _results_page(20, start=0)

    with _fake_get(handler):
        jobs = fetch_werkenvoornederland("RIVM", {"werkgever": "01760", "descriptions": False,
                                                   "max_results": 5})
    assert len(jobs) == 5
    print("PASS test_fetch_werkenvoornederland_respects_max_results")


def test_fetch_werkenvoornederland_fetches_description():
    # Detail pages carry several same-class content blocks (duties,
    # requirements, employer blurb, ...) -- all of them must end up in the
    # description, not just the first.
    def handler(url):
        if "_hn:type=resource" in url:
            return _results_page(1)
        return DETAIL_HTML  # detail-page fetch

    with _fake_get(handler):
        jobs = fetch_werkenvoornederland("RIVM", {"werkgever": "01760", "descriptions": True})
    assert len(jobs) == 1
    assert "duties text" in jobs[0].description
    assert "requirements text" in jobs[0].description
    assert "employer blurb" in jobs[0].description
    print("PASS test_fetch_werkenvoornederland_fetches_description")


def test_fetch_werkenvoornederland_detail_failure_leaves_description_empty():
    # A RequestException on the detail fetch must not crash the whole run --
    # the job still comes back, just without a description.
    import requests as requests_mod

    def handler(url):
        if "_hn:type=resource" in url:
            return _results_page(1)
        raise requests_mod.RequestException("boom")

    with _fake_get(handler):
        jobs = fetch_werkenvoornederland("RIVM", {"werkgever": "01760", "descriptions": True})
    assert len(jobs) == 1
    assert jobs[0].description == ""
    print("PASS test_fetch_werkenvoornederland_detail_failure_leaves_description_empty")


def test_fetch_werkenvoornederland_skips_card_with_no_link():
    # A card with no <a data-govan-action="view-card"> has no detail url to
    # dedupe or fetch on -- it must be dropped, not turned into a job with an
    # empty job_id that collides with every other such card.
    html = """
    <li><section class="vacancy">
    <h2 class="vacancy__title"></h2>
    <p class="vacancy__employer">RIVM</p>
    </section></li>
    """

    def handler(url):
        return html

    with _fake_get(handler):
        jobs = fetch_werkenvoornederland("RIVM", {"werkgever": "01760", "descriptions": False})
    assert jobs == []
    print("PASS test_fetch_werkenvoornederland_skips_card_with_no_link")


if __name__ == "__main__":
    test_parse_werkenvoornederland_card()
    test_fetch_werkenvoornederland_no_werkgever_returns_empty()
    test_fetch_werkenvoornederland_passes_werkgever_param()
    test_fetch_werkenvoornederland_stops_on_short_page()
    test_fetch_werkenvoornederland_paginates_on_full_page()
    test_fetch_werkenvoornederland_respects_max_results()
    test_fetch_werkenvoornederland_fetches_description()
    test_fetch_werkenvoornederland_detail_failure_leaves_description_empty()
    test_fetch_werkenvoornederland_skips_card_with_no_link()
    print("\nAll werkenvoornederland adapter tests passed.")
