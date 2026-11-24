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
from jobtracker.adapters import (
    fetch_werkenvoornederland,
    parse_werkenvoornederland_card,
    _wvn_detail_description,
)


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


# Sized like a real posting: duties alone (2.6-4.2k chars on real pages) can
# already exceed a naive overall cap, pushing requirements out entirely if
# everything were just joined in DOM order and cut once at the end. The
# un-anchored "benefits" block mirrors the ~800-char boilerplate every real
# posting repeats identically -- it must NOT survive into the description.
_DUTIES_FILLER = ("Duties filler text describing the role in detail. " * 60)[:3000]
_BENEFITS_BOILERPLATE = ("Boilerplate IKB benefits text identical on every posting. " * 15)[:800]
_REQUIREMENTS_FILLER = ("Requirements filler text describing what we ask. " * 40)[:1800]

DETAIL_HTML = f"""
<html><body>
<section class="contrast--background" id="dit_ga_je_doen_anchor">
  <div class="s-article-content"><p>DUTIES_START {_DUTIES_FILLER} DUTIES_END</p></div>
</section>
<section class="some-other-section">
  <div class="s-article-content"><p>BENEFITS_START {_BENEFITS_BOILERPLATE} BENEFITS_END</p></div>
</section>
<section class="contrast--background" id="dit_vragen_wij_anchor">
  <div class="s-article-content"><p>REQUIREMENTS_START {_REQUIREMENTS_FILLER} REQUIREMENTS_END</p></div>
</section>
<section class="contrast--background" id="bijzonderheden_anchor">
  <div class="s-article-content"><p>BIJZONDERHEDEN_START filler BIJZONDERHEDEN_END</p></div>
</section>
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


def test_wvn_detail_description_keeps_requirements_despite_long_duties():
    # Regression test for the actual bug: joining every content block in DOM
    # order (duties -> benefits boilerplate -> requirements -> ...) and
    # cutting once at the overall budget buried requirements entirely on
    # most real postings, since duties alone (2.6-4.2k chars on real pages)
    # already exceeds a naive cap. Pulling duties/requirements from their own
    # stable section anchors and splitting the budget between them means
    # requirements always survives, regardless of how long duties runs.
    desc = _wvn_detail_description(DETAIL_HTML)
    assert "DUTIES_START" in desc
    # requirements' own 1600-char budget is shorter than the 1800-char filler,
    # so it's truncated too -- the point is that it's PRESENT at all, which
    # the old DOM-order-then-cut approach failed on for most real postings.
    assert "REQUIREMENTS_START" in desc
    assert "BENEFITS_START" not in desc  # un-anchored boilerplate excluded
    assert "BIJZONDERHEDEN_START" not in desc  # not one of the two pulled sections
    assert len(desc) <= adapters_mod.WVN_DUTIES_CHARS + adapters_mod.WVN_REQUIREMENTS_CHARS + 1
    print("PASS test_wvn_detail_description_keeps_requirements_despite_long_duties")


def test_fetch_werkenvoornederland_fetches_description():
    def handler(url):
        if "_hn:type=resource" in url:
            return _results_page(1)
        return DETAIL_HTML  # detail-page fetch

    with _fake_get(handler):
        jobs = fetch_werkenvoornederland("RIVM", {"werkgever": "01760", "descriptions": True})
    assert len(jobs) == 1
    assert "DUTIES_START" in jobs[0].description
    assert "REQUIREMENTS_START" in jobs[0].description
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
    test_wvn_detail_description_keeps_requirements_despite_long_duties()
    test_fetch_werkenvoornederland_fetches_description()
    test_fetch_werkenvoornederland_detail_failure_leaves_description_empty()
    test_fetch_werkenvoornederland_skips_card_with_no_link()
    print("\nAll werkenvoornederland adapter tests passed.")
