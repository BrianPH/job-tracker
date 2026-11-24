"""Offline tests for tools/check_source.py (no network -- requests.get is swapped out)."""
import sys
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

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
def _fake_get(text: str):
    original = check_source.requests.get
    check_source.requests.get = lambda *a, **k: _FakeResponse(text)
    try:
        yield
    finally:
        check_source.requests.get = original


def test_check_source_counts_matched_links():
    with _fake_get(FIXTURE_HTML):
        links = check_source.check_source("https://example.com/careers", "/vacancies/.")
    assert len(links) == 2
    urls = [u for _, u in links]
    assert "https://example.com/vacancies/data-engineer" in urls
    assert all("/about" not in u for u in urls)
    print("PASS test_check_source_counts_matched_links")


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


if __name__ == "__main__":
    test_check_source_counts_matched_links()
    test_main_exits_nonzero_on_no_links()
    test_main_exits_zero_on_match()
    print("\nAll check_source tests passed.")
