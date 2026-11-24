"""Verify a careers page before adding it to config/companies.yaml.

Fetches the page with the same headers the html adapter uses, extracts
vacancy links with the same logic as jobtracker.adapters.fetch_html, and
reports how many were found. A pattern that matches zero links usually
means the page is JS-rendered (empty/near-empty static HTML) rather than
that the pattern is wrong -- check the raw response in that case.

Usage:
    python tools/check_source.py <careers_url> [link_pattern]

Exit code is non-zero when no vacancy links are found, so this can gate a
companies.yaml entry in a quick pass/fail check.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import List, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import requests

from jobtracker.adapters import BROWSER_HEADERS, TIMEOUT, extract_html_links


def check_source(url: str, link_pattern: str = "") -> List[Tuple[str, str]]:
    """Fetch `url` and return the (title, url) pairs extract_html_links finds."""
    resp = requests.get(url, headers=BROWSER_HEADERS, timeout=TIMEOUT)
    resp.raise_for_status()
    return extract_html_links(resp.text, url, link_pattern)


def main(argv: List[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    url = argv[0]
    link_pattern = argv[1] if len(argv) > 1 else ""

    try:
        links = check_source(url, link_pattern)
    except requests.RequestException as e:
        print(f"FETCH ERROR: {e}")
        return 1

    print(f"{url}")
    print(f"  pattern: {link_pattern or '(generic heuristic)'}")
    print(f"  matched links: {len(links)}")
    for title, link_url in links[:10]:
        print(f"    - {title!r} -> {link_url}")
    if len(links) > 10:
        print(f"    ... and {len(links) - 10} more")

    if not links:
        print("  NO LINKS FOUND -- likely JS-rendered, or the pattern needs adjusting.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
