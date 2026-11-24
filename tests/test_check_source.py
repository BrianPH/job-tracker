"""Offline tests for check_source.py (no network -- requests.get is swapped out)."""
import sys
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import check_source  # noqa: E402


class _FakeResponse:
    def __init__(self, text: str):
        self.text = text

    def raise_for_status(self):
        pass


FIXTURE_HTML = """
<html><body>
  <a href="/vacancies/data-engineer">Data Engineer</a>
  <a href="/about">About us</a>
  <a href="/vacancies/research-software-engineer">Research Software Engineer</a>
</body></html>
"""


@contextmanager
def _fake_get(text: str = None, raises: Exception = None):
    original = check_source.requests.get

    def _fake(*a, **k):
        if raises is not None:
            raise raises
        return _FakeResponse(text)

    check_source.requests.get = _fake
    try:
        yield
    finally:
        check_source.requests.get = original


def test_check_source_counts_matched_links():
    with _fake_get(FIXTURE_HTML):
        links, page_html = check_source.check_source("https://example.com/careers", "/vacancies/.")
    assert len(links) == 2
    assert page_html == FIXTURE_HTML
    urls = [u for _, u in links]
    assert "https://example.com/vacancies/data-engineer" in urls
    assert all("/about" not in u for u in urls)
    print("PASS test_check_source_counts_matched_links")


def test_check_source_default_generic_heuristic():
    # link_pattern omitted entirely -> falls through to extract_html_links'
    # generic keyword heuristic (matches "job"/"vacancy"/"career"/...), same
    # as fetch_html's default behaviour. FIXTURE_HTML's "/vacancies/" URLs
    # don't contain any of those exact keywords, so use a matching fixture.
    html = """
    <html><body>
      <a href="/jobs/data-engineer">Data Engineer</a>
      <a href="/about">About us</a>
    </body></html>
    """
    with _fake_get(html):
        links, _ = check_source.check_source("https://example.com/careers")
    urls = [u for _, u in links]
    assert any("jobs" in u for u in urls)
    assert all("/about" not in u for u in urls)
    print("PASS test_check_source_default_generic_heuristic")


def test_main_exits_nonzero_on_no_links():
    with _fake_get("<html><body>no links here</body></html>"):
        rc = check_source.main(["https://example.com/careers", "/vacancies/."])
    assert rc == 1
    print("PASS test_main_exits_nonzero_on_no_links")


def test_main_exits_zero_on_match():
    with _fake_get(FIXTURE_HTML):
        rc = check_source.main(["https://example.com/careers", "/vacancies/."])
    assert rc == 0
    print("PASS test_main_exits_zero_on_match")


def test_main_exits_one_on_fetch_error():
    with _fake_get(raises=check_source.requests.RequestException("boom")):
        rc = check_source.main(["https://example.com/careers", "/vacancies/."])
    assert rc == 1
    print("PASS test_main_exits_one_on_fetch_error")


def test_main_exits_two_on_missing_args():
    assert check_source.main([]) == 2
    print("PASS test_main_exits_two_on_missing_args")


def test_main_exits_two_on_invalid_pattern():
    with _fake_get(FIXTURE_HTML):
        rc = check_source.main(["https://example.com/careers", "("])  # unbalanced group
    assert rc == 2
    print("PASS test_main_exits_two_on_invalid_pattern")


if __name__ == "__main__":
    test_check_source_counts_matched_links()
    test_check_source_default_generic_heuristic()
    test_main_exits_nonzero_on_no_links()
    test_main_exits_zero_on_match()
    test_main_exits_one_on_fetch_error()
    test_main_exits_two_on_missing_args()
    test_main_exits_two_on_invalid_pattern()
    print("\nAll check_source tests passed.")
