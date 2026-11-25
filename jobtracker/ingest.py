"""Ingest a single vacancy from a bare URL (or pasted text) for CV/letter
generation, instead of requiring it to already be a scraped `Job`.

Order of attempts, matching issue #8's scope:
  1. If the URL matches a platform we already have an adapter for (Greenhouse,
     Lever, Ashby, SmartRecruiters, Teamtailor, Workday, Recruitee), call that
     platform's own single-posting detail endpoint directly and parse it with
     the SAME parse_* function the bulk adapter uses in adapters.py.
  2. Otherwise, fetch the page and extract the main content with trafilatura
     (strips navigation/footer/cookie-banner chrome).
  3. If the extracted text is too short (typically a client-rendered SPA),
     optionally retry with Playwright (only if PLAYWRIGHT_ENABLED=1 and the
     `playwright` package is installed) — otherwise raise ThinContentError so
     the caller can ask the user to paste the vacancy text instead.
  4. Pasted text always short-circuits straight to a Job, no network.

Every successful path writes a row to vacancy_snapshots (see snapshots.py)
keyed by URL, so the text survives the posting going offline.
"""
from __future__ import annotations

import re
from typing import Callable, Optional, Tuple

import requests

from . import adapters
from .models import Job
from .snapshots import make_snapshot_store

MIN_EXTRACTED_CHARS = 200


class ThinContentError(Exception):
    """Raised when a generic page's extracted text is too short to be useful
    (almost always a client-rendered page the static fetch couldn't see)."""

    def __init__(self, url: str, raw_html: str):
        super().__init__(f"Extracted text too short for {url} — likely client-rendered; "
                          "paste the vacancy text instead.")
        self.url = url
        self.raw_html = raw_html


def _title_case_slug(slug: str) -> str:
    return re.sub(r"[-_]+", " ", slug).strip().title() or slug


# --------------------------------------------------------------------------- #
# Known-platform detail re-fetch. Each entry: (platform name, URL regex,      #
# detail-fetch function taking the regex Match and returning a Job).          #
# --------------------------------------------------------------------------- #
def _detail_greenhouse(m: "re.Match") -> Job:
    board, job_id = m.group(1), m.group(2)
    url = f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs/{job_id}?content=true"
    resp = requests.get(url, headers=adapters.BROWSER_HEADERS, timeout=adapters.TIMEOUT)
    resp.raise_for_status()
    return adapters.parse_greenhouse_job(resp.json(), _title_case_slug(board)), resp.text


def _detail_lever(m: "re.Match") -> Job:
    slug, posting_id = m.group(1), m.group(2)
    url = f"https://api.lever.co/v0/postings/{slug}/{posting_id}?mode=json"
    resp = requests.get(url, headers=adapters.BROWSER_HEADERS, timeout=adapters.TIMEOUT)
    resp.raise_for_status()
    return adapters.parse_lever_posting(resp.json(), _title_case_slug(slug)), resp.text


def _detail_ashby(m: "re.Match") -> Job:
    token, posting_id = m.group(1), m.group(2)
    url = f"https://api.ashbyhq.com/posting-api/job-board/{token}"
    resp = requests.get(url, headers=adapters.BROWSER_HEADERS, timeout=adapters.TIMEOUT)
    resp.raise_for_status()
    jobs = resp.json().get("jobs", [])
    item = next((j for j in jobs if j.get("id") == posting_id), None)
    if item is None:
        raise ValueError(f"Ashby posting {posting_id} not found on board {token}")
    return adapters.parse_ashby_job(item, _title_case_slug(token)), resp.text


def _detail_smartrecruiters(m: "re.Match") -> Job:
    company_id, posting_id = m.group(1), m.group(2)
    url = f"https://api.smartrecruiters.com/v1/companies/{company_id}/postings/{posting_id}"
    resp = requests.get(url, headers=adapters.BROWSER_HEADERS, timeout=adapters.TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    job = adapters.parse_smartrecruiters_posting(data, _title_case_slug(company_id), company_id)
    adapters.apply_smartrecruiters_detail(job, data)
    return job, resp.text


def _detail_teamtailor(m: "re.Match") -> Job:
    site, posting_id = m.group(1), m.group(2)
    url = f"https://{site}/jobs.json"
    resp = requests.get(url, headers={"User-Agent": adapters.HEADERS["User-Agent"],
                                      "Accept": "application/feed+json, application/json"},
                        timeout=adapters.TIMEOUT)
    resp.raise_for_status()
    items = resp.json().get("items", [])
    company = _title_case_slug(site.split(".")[0])
    for item in items:
        item_url = item.get("url") or ""
        im = re.search(r"/jobs/(\d+)-", item_url)
        if im and im.group(1) == posting_id:
            return adapters.parse_teamtailor_item(item, company), resp.text
    raise ValueError(f"Teamtailor posting {posting_id} not found on {site}")


def _detail_workday(m: "re.Match") -> Job:
    host, site, external_path = m.group(1), m.group(2), m.group(3)
    tenant = host.split(".")[0]
    detail_url = f"https://{host}/wday/cxs/{tenant}/{site}{external_path}"
    resp = requests.get(detail_url, headers=adapters.BROWSER_HEADERS, timeout=adapters.TIMEOUT)
    resp.raise_for_status()
    info = resp.json().get("jobPostingInfo", {})
    job = Job(
        title=(info.get("title") or "").strip(),
        company=_title_case_slug(tenant),
        source="Workday",
        url=f"https://{host}/{site}{external_path}",
        job_id=f"workday:{tenant}:{external_path.rsplit('/', 1)[-1]}",
    )
    adapters.apply_workday_detail(job, info)
    return job, resp.text


def _detail_recruitee(m: "re.Match") -> Job:
    slug, offer_slug = m.group(1), m.group(2)
    url = f"https://{slug}.recruitee.com/api/offers/{offer_slug}"
    resp = requests.get(url, headers=adapters.HEADERS, timeout=adapters.TIMEOUT)
    resp.raise_for_status()
    offer = resp.json().get("offer", {})
    return adapters.parse_recruitee_offer(offer, _title_case_slug(slug)), resp.text


# (platform, regex, detail-fetch function). Order matters only in that the
# first matching pattern wins; the patterns themselves don't overlap.
PLATFORM_PATTERNS: Tuple[Tuple[str, "re.Pattern", Callable], ...] = (
    ("greenhouse", re.compile(r"(?:boards|job-boards)\.greenhouse\.io/([\w-]+)/jobs/(\d+)"),
     _detail_greenhouse),
    ("lever", re.compile(r"jobs\.lever\.co/([\w-]+)/([0-9a-fA-F-]{36})"),
     _detail_lever),
    ("ashby", re.compile(r"jobs\.ashbyhq\.com/([\w-]+)/([0-9a-fA-F-]{36})"),
     _detail_ashby),
    ("smartrecruiters", re.compile(r"jobs\.smartrecruiters\.com/([\w-]+)/([\w]+)"),
     _detail_smartrecruiters),
    ("teamtailor", re.compile(r"([\w.-]+\.teamtailor\.com)/jobs/(\d+)-"),
     _detail_teamtailor),
    ("workday", re.compile(r"([\w-]+\.wd\d+\.myworkdayjobs\.com)/(?:[\w-]+/)*([\w-]+)/job(/\S+)"),
     _detail_workday),
    ("recruitee", re.compile(r"([\w-]+)\.recruitee\.com/o/([\w-]+)"),
     _detail_recruitee),
)


def detect_platform(url: str) -> Optional[str]:
    for name, pattern, _ in PLATFORM_PATTERNS:
        if pattern.search(url):
            return name
    return None


def _fetch_known_platform(url: str) -> Optional[Tuple[str, Job, str]]:
    """Returns (platform, job, raw_json_text) for the first matching pattern,
    or None if no pattern matches. Lets network/parsing errors propagate —
    the caller decides whether to fall back to the generic path."""
    for name, pattern, detail_fn in PLATFORM_PATTERNS:
        m = pattern.search(url)
        if m:
            job, raw = detail_fn(m)
            return name, job, raw
    return None


# --------------------------------------------------------------------------- #
# Generic fallback: fetch + trafilatura extraction, optional Playwright       #
# --------------------------------------------------------------------------- #
def fetch_generic(url: str) -> Tuple[str, str]:
    """Returns (raw_html, extracted_text)."""
    resp = requests.get(url, headers=adapters.BROWSER_HEADERS, timeout=adapters.TIMEOUT)
    resp.raise_for_status()
    raw_html = resp.text
    import trafilatura
    extracted = trafilatura.extract(raw_html, url=url) or ""
    return raw_html, extracted


def fetch_with_playwright(url: str) -> Optional[str]:
    """Best-effort JS render for client-rendered pages. Only runs when
    PLAYWRIGHT_ENABLED=1 and the playwright package is installed; otherwise
    returns None so the caller falls back to asking for pasted text."""
    import os
    if os.getenv("PLAYWRIGHT_ENABLED") != "1":
        return None
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page()
            page.goto(url, timeout=30000, wait_until="networkidle")
            return page.content()
        finally:
            browser.close()


def _title_from_html(raw_html: str, extracted_text: str) -> str:
    m = re.search(r"<title[^>]*>(.*?)</title>", raw_html, re.IGNORECASE | re.DOTALL)
    if m:
        title = re.sub(r"\s+", " ", m.group(1)).strip()
        if title:
            return title
    first_line = (extracted_text or "").strip().splitlines()
    return first_line[0][:200] if first_line else "Vacancy"


def _job_from_text(url: str, title: str, description: str, source: str) -> Job:
    return Job(
        title=title,
        company="",
        source=source,
        url=url,
        description=description,
        job_id=Job.make_id("ingest", url),
    )


# --------------------------------------------------------------------------- #
# Orchestration                                                               #
# --------------------------------------------------------------------------- #
def ingest_url(url: str, paste_text: Optional[str] = None, snapshot_store=None) -> dict:
    """Ingest one vacancy and return a Job-shaped dict ready for
    generator.generate_application(). Always writes a vacancy_snapshots row
    (except when neither a URL nor a paste is given, which raises)."""
    url = (url or "").strip()
    paste_text = (paste_text or "").strip()
    if not url and not paste_text:
        raise ValueError("A vacancy URL or pasted text is required.")
    store = snapshot_store if snapshot_store is not None else make_snapshot_store()

    if paste_text:
        job = _job_from_text(url or "pasted-text", _title_from_html("", paste_text),
                             paste_text, "Pasted")
        store.save(url or job.job_id, job_id=job.job_id, platform="paste",
                  raw_source="", extracted_text=paste_text)
        return job.to_record()

    known = _fetch_known_platform(url)
    if known is not None:
        platform, job, raw = known
        store.save(url, job_id=job.job_id, platform=platform,
                  raw_source=raw, extracted_text=job.description)
        return job.to_record()

    raw_html, extracted = fetch_generic(url)
    if len(extracted) < MIN_EXTRACTED_CHARS:
        pw_html = fetch_with_playwright(url)
        if pw_html:
            import trafilatura
            pw_text = trafilatura.extract(pw_html, url=url) or ""
            if len(pw_text) >= MIN_EXTRACTED_CHARS:
                raw_html, extracted = pw_html, pw_text
    if len(extracted) < MIN_EXTRACTED_CHARS:
        raise ThinContentError(url, raw_html)

    job = _job_from_text(url, _title_from_html(raw_html, extracted), extracted, "Ingested")
    store.save(url, job_id=job.job_id, platform="html",
              raw_source=raw_html, extracted_text=extracted)
    return job.to_record()
