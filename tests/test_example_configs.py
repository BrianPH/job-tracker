"""Offline tests: the example config files (config/*.example.yaml) must parse
and have the shape pipeline.py / generator.py / webapp.py expect, since they
are the fallback loaded when the real (git-ignored) config files are absent.
No network required.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import yaml

ROOT = Path(__file__).resolve().parents[1]
COMPANIES_EXAMPLE = ROOT / "config" / "companies.example.yaml"
PROFILE_EXAMPLE = ROOT / "config" / "profile.example.yaml"


def test_profile_example_parses_with_expected_shape():
    data = yaml.safe_load(PROFILE_EXAMPLE.read_text(encoding="utf-8"))
    assert data["profile"]["summary"]
    assert data["personas"] and data["personas"][0]["name"]
    rules = data["rules"]
    assert isinstance(rules["exclude_any"], list) and "phd" in rules["exclude_any"]
    assert isinstance(rules["locations_any"], list) and "Amsterdam" in rules["locations_any"]
    assert isinstance(rules["always_keep_remote"], bool)
    assert isinstance(data["app"]["scrape_hour"], int)
    print("PASS test_profile_example_parses_with_expected_shape")


def test_companies_example_parses_with_expected_shape():
    data = yaml.safe_load(COMPANIES_EXAMPLE.read_text(encoding="utf-8"))
    companies = data["companies"]
    assert len(companies) >= 5
    names = {c["name"] for c in companies}
    assert {"TU Delft", "KNAW", "Leiden University", "Erasmus MC",
            "Naturalis Biodiversity Center"} <= names
    for c in companies:
        assert c["adapter"] == "html"
        params = c["params"]
        assert params["careers_url"].startswith("https://")
        assert params["link_pattern"]
        # title_include/title_exclude are the whole point of this file --
        # every entry must actually set them, not just fetch_html supporting them.
        assert params["title_include"], c["name"]
        assert params["title_exclude"], c["name"]
    print("PASS test_companies_example_parses_with_expected_shape")


if __name__ == "__main__":
    test_profile_example_parses_with_expected_shape()
    test_companies_example_parses_with_expected_shape()
    print("\nAll example-config tests passed.")
