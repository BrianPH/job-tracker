"""Verify a careers page before adding it to config/companies.yaml.

Fetches the page with the same headers the html adapter uses, and extracts
vacancy links with the same link-matching logic as
jobtracker.adapters.fetch_html (fetch_html additionally caps the result at
`max_results`, which this tool does not -- it reports every match so you can
judge the pattern). A pattern that matches zero links can mean either the
page is JS-rendered (little or no real content in the static HTML) or that
the pattern itself is wrong; the no-match output below prints enough of the
raw response to tell the two apart.

Usage:
    python check_source.py <careers_url> [link_pattern]

Exit code is non-zero when no vacancy links are found or the arguments are
invalid, so this can gate a companies.yaml entry in a quick pass/fail check.
"""
from __future__ import annotations

import re
import sys
from typing import List, Tuple

import requests

from jobtracker.adapters import BROWSER_HEADERS, TIMEOUT, extract_html_links


def check_source(url: str, link_pattern: str = "") -> Tuple[List[Tuple[str, str]], str]:
    """Fetch `url` and return (matched (title, url) pairs, raw response text)."""
    resp = requests.get(url, headers=BROWSER_HEADERS, timeout=TIMEOUT)
    resp.raise_for_status()
    links = extract_html_links(resp.text, url, link_pattern)
    return links, resp.text


def _total_anchor_count(page_html: str) -> int:
    from bs4 import BeautifulSoup
    return len(BeautifulSoup(page_html, "html.parser").find_all("a", href=True))


def main(argv: List[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    url = argv[0]
    link_pattern = argv[1] if len(argv) > 1 else ""

    try:
        links, page_html = check_source(url, link_pattern)
    except requests.RequestException as e:
        print(f"FETCH ERROR: {e}")
        return 1
    except re.error as e:
        print(f"INVALID link_pattern {link_pattern!r}: {e}")
        return 2

    print(f"{url}")
    print(f"  pattern: {link_pattern or '(generic heuristic)'}")
    print(f"  matched links: {len(links)}")
    for title, link_url in links[:10]:
        print(f"    - {title!r} -> {link_url}")
    if len(links) > 10:
        print(f"    ... and {len(links) - 10} more")

    if not links:
        print(f"  NO LINKS FOUND -- response was {len(page_html)} chars with "
              f"{_total_anchor_count(page_html)} anchor tags total.")
        print("  Zero anchors despite a non-trivial response usually means "
              "JS-rendered content; anchors present but none matching usually "
              "means the pattern needs adjusting -- inspect the raw response.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
