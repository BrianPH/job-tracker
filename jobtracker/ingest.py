"""Ingest a single vacancy from a bare URL (or pasted text) for CV/letter
generation, instead of requiring it to already be a scraped `Job`.

Order of attempts, matching issue #8's scope:
  1. If the URL matches a platform we already have an adapter for (Greenhouse,
     Lever, Ashby, SmartRecruiters, Teamtailor, Workday, Recruitee), call that
     platform's own single-posting detail endpoint directly and parse it with
     the SAME parse_* function the bulk adapter uses in adapters.py. If that
     detail fetch fails (wrong/stale id, 404, a URL that only *looks* like the
     platform's pattern — e.g. an "Apply" link rather than a listing link),
     fall through to step 2 rather than giving up.
  2. Fetch the page and extract the main content with trafilatura (strips
     navigation/footer/cookie-banner chrome).
  3. If the extracted text is too short (typically a client-rendered SPA),
     optionally retry with Playwright (only if PLAYWRIGHT_ENABLED=1 and the
     `playwright` package is installed) — otherwise raise ThinContentError so
     the caller can ask the user to paste the vacancy text instead.
  4. Pasted text always short-circuits straight to a Job, no network.

Every attempt writes a row to vacancy_snapshots (see snapshots.py) keyed by
URL, so the text survives the posting going offline — including a thin-content
failure, which still saves the raw HTML that was too short to extract from,
in case a human wants to look at it later.
"""
from __future__ import annotations

import os
import re
from typing import Callable, Optional, Tuple
from urllib.parse import urlparse

import requests
import trafilatura

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


class IngestInputError(ValueError):
    """Raised for a user-input problem that's safe to show verbatim (currently
    just "neither a URL nor pasted text was given"). Deliberately NOT the base
    for anything requests.* raises -- several of its exceptions (e.g.
    MissingSchema for a URL pasted without "https://") also subclass
    ValueError, and those carry library-internal wording that shouldn't be
    reflected onto the page; callers should catch THIS type specifically,
    not bare ValueError, to tell the two apart."""


def _title_case_slug(slug: str) -> str:
    return re.sub(r"[-_]+", " ", slug).strip().title() or slug


# --------------------------------------------------------------------------- #
# Known-platform detail re-fetch. Each entry: (platform name, URL regex,      #
# detail-fetch function taking the regex Match and returning (Job, raw)).     #
# --------------------------------------------------------------------------- #
def _detail_greenhouse(m: "re.Match") -> Tuple[Job, str]:
    board, job_id = m.group(1), m.group(2)
    url = f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs/{job_id}?content=true"
    resp = requests.get(url, headers=adapters.BROWSER_HEADERS, timeout=adapters.TIMEOUT)
    resp.raise_for_status()
    return adapters.parse_greenhouse_job(resp.json(), _title_case_slug(board)), resp.text


def _detail_lever(m: "re.Match") -> Tuple[Job, str]:
    slug, posting_id = m.group(1), m.group(2)
    url = f"https://api.lever.co/v0/postings/{slug}/{posting_id}?mode=json"
    resp = requests.get(url, headers=adapters.BROWSER_HEADERS, timeout=adapters.TIMEOUT)
    resp.raise_for_status()
    return adapters.parse_lever_posting(resp.json(), _title_case_slug(slug)), resp.text


def _detail_ashby(m: "re.Match") -> Tuple[Job, str]:
    token, posting_id = m.group(1), m.group(2)
    url = f"https://api.ashbyhq.com/posting-api/job-board/{token}"
    resp = requests.get(url, headers=adapters.BROWSER_HEADERS, timeout=adapters.TIMEOUT)
    resp.raise_for_status()
    jobs = resp.json().get("jobs", [])
    item = next((j for j in jobs if j.get("id") == posting_id), None)
    if item is None:
        raise ValueError(f"Ashby posting {posting_id} not found on board {token}")
    return adapters.parse_ashby_job(item, _title_case_slug(token)), resp.text


def _detail_smartrecruiters(m: "re.Match") -> Tuple[Job, str]:
    company_id, posting_id = m.group(1), m.group(2)
    url = f"https://api.smartrecruiters.com/v1/companies/{company_id}/postings/{posting_id}"
    resp = requests.get(url, headers=adapters.BROWSER_HEADERS, timeout=adapters.TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    job = adapters.parse_smartrecruiters_posting(data, _title_case_slug(company_id), company_id)
    adapters.apply_smartrecruiters_detail(job, data)
    return job, resp.text


def _detail_teamtailor(m: "re.Match") -> Tuple[Job, str]:
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


def _detail_workday(m: "re.Match") -> Tuple[Job, str]:
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


def _detail_recruitee(m: "re.Match") -> Tuple[Job, str]:
    slug, offer_slug = m.group(1), m.group(2)
    url = f"https://{slug}.recruitee.com/api/offers/{offer_slug}"
    resp = requests.get(url, headers=adapters.HEADERS, timeout=adapters.TIMEOUT)
    resp.raise_for_status()
    offer = resp.json().get("offer", {})
    return adapters.parse_recruitee_offer(offer, _title_case_slug(slug)), resp.text


# (platform, regex, detail-fetch function). Order matters only in that the
# first matching pattern wins; the patterns themselves don't overlap.
# Workday's third group starts with the literal "/job" (not just what follows
# it) and excludes "?"/"#" so apply-tracking query strings never leak into the
# built detail URL or the job_id.
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
    ("workday",
     re.compile(r"([\w-]+\.wd\d+\.myworkdayjobs\.com)/(?:[\w-]+/)*([\w-]+)(/job/[^\s?#]+)"),
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
    or None if no pattern matches. A matched pattern whose actual fetch/parse
    fails (RequestException, or ValueError for a posting id that isn't on the
    board/site after all) also returns None, so the caller falls through to
    the generic fetch+trafilatura path instead of giving up outright — a URL
    can look like a known platform's pattern (e.g. an "Apply" link with a
    different id shape) without that platform's detail call actually working."""
    for name, pattern, detail_fn in PLATFORM_PATTERNS:
        m = pattern.search(url)
        if m:
            try:
                job, raw = detail_fn(m)
            except (requests.RequestException, ValueError) as e:
                print(f"  [ingest] {name} detail fetch failed for {url!r} ({e}); "
                     "falling back to generic fetch")
                return None
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
    extracted = trafilatura.extract(raw_html, url=url) or ""
    return raw_html, extracted


def fetch_with_playwright(url: str) -> Optional[str]:
    """Best-effort JS render for client-rendered pages. Only runs when
    PLAYWRIGHT_ENABLED=1 and the playwright package is installed; otherwise
    returns None so the caller falls back to asking for pasted text."""
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


_PIPE_SEP_RE = re.compile(r"\s*\|\s*")
_DASH_SEP_RE = re.compile(r"\s+[\-–—]\s+")
_BOILERPLATE_TAIL_RE = re.compile(
    r"^(careers?|jobs?|vacature(n)?|vacancies|werken\s+bij)\b", re.IGNORECASE)
_LEADING_COMPANY_BOILERPLATE_RE = re.compile(
    r"^(careers?\s+at|jobs?\s+at|werken\s+bij|vacatures?\s+bij)\s+", re.IGNORECASE)
_TRAILING_COMPANY_BOILERPLATE_RE = re.compile(
    r"[\s|\-–—]+(careers?|jobs?|vacatures?)\s*$", re.IGNORECASE)
# A sitename that is itself just a bare domain (no spaces, dot-separated
# labels) carries no real signal -- trafilatura falls back to the hostname
# (stripping a leading "www."/"w1." etc.) when a page has no og:site_name,
# so comparing against the exact original host misses that case.
_BARE_DOMAIN_RE = re.compile(r"^[\w-]+(\.[\w-]+)+$")
# Second-level labels that are themselves a generic suffix, not the company
# name, for domains like "jobs.ox.ac.uk" or "careers.acme.co.uk" -- not a
# full public-suffix list, just the common ones relevant to this project's
# (Dutch/European) target sources.
_SECOND_LEVEL_SUFFIXES = {"co", "ac", "org", "gov", "edu", "net", "sch"}


def _clean_title(raw_title: str, company: str = "") -> str:
    """Career-page <title> tags are conventionally "Role Title | Company
    Careers" or "Role Title - Company". "|" is essentially never part of a
    real job title, so always split on it. A dash/em-dash tail is common in
    LEGITIMATE titles too (Dutch titles especially: "Senior Onderzoeker —
    afdeling Genomics"), so that split only applies when the tail actually
    looks like boilerplate or repeats the resolved company name -- otherwise
    an unconditional split would wrongly truncate those. A hyphen inside a
    word (e.g. "Full-Stack") never matches the dash separator, which
    requires surrounding whitespace."""
    if not raw_title:
        return raw_title
    title = raw_title.strip()

    pipe_parts = _PIPE_SEP_RE.split(title, maxsplit=1)
    if len(pipe_parts) > 1 and pipe_parts[0].strip():
        title = pipe_parts[0].strip()

    dash_parts = _DASH_SEP_RE.split(title, maxsplit=1)
    if len(dash_parts) > 1:
        head, tail = dash_parts[0].strip(), dash_parts[1].strip()
        tail_norm = tail.lower()
        looks_boilerplate = bool(_BOILERPLATE_TAIL_RE.match(tail_norm))
        repeats_company = bool(company) and company.lower() in tail_norm
        if head and (looks_boilerplate or repeats_company):
            title = head

    return title or raw_title


def _title_from_html(raw_html: str, extracted_text: str) -> str:
    m = re.search(r"<title[^>]*>(.*?)</title>", raw_html, re.IGNORECASE | re.DOTALL)
    if m:
        title = re.sub(r"\s+", " ", m.group(1)).strip()
        if title:
            return title
    first_line = (extracted_text or "").strip().splitlines()
    return first_line[0][:200] if first_line else "Vacancy"


def _looks_like_bare_domain(s: str) -> bool:
    return bool(s) and " " not in s and bool(_BARE_DOMAIN_RE.match(s))


def _company_from_domain(url: str) -> str:
    """Best-effort company guess from the registrable domain when no better
    signal (og:site_name, JSON-LD) is available — e.g. "careers.acme.com" or
    "acme.io" both -> "Acme", "jobs.ox.ac.uk" -> "Ox". Approximate (not a
    real public-suffix list, so a bare two-label ccTLD domain like "uu.nl"
    still yields just "Uu"), good enough as a fallback, never used for a
    known platform where the real company name/slug is already available."""
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""
    parts = [p for p in host.split(".") if p]
    if len(parts) < 2:
        return _title_case_slug(host)
    if len(parts) >= 3 and parts[-2] in _SECOND_LEVEL_SUFFIXES:
        return _title_case_slug(parts[-3])
    return _title_case_slug(parts[-2])


def _strip_company_boilerplate(name: str) -> str:
    """A genuine og:site_name routinely carries the same boilerplate
    _clean_title strips from titles -- "Jobs at Acme", "Werken bij Acme",
    "Acme | Careers", "Acme Labs Careers" should all resolve to just the
    company. Unlike _clean_title's title-tail stripping, there's no
    "repeats the company" signal to gate on here (this IS the company
    value), so strip unconditionally."""
    if not name:
        return name
    stripped = _LEADING_COMPANY_BOILERPLATE_RE.sub("", name)
    stripped = _TRAILING_COMPANY_BOILERPLATE_RE.sub("", stripped).strip()
    return stripped or name.strip()


def _title_and_company_from_html(raw_html: str, extracted_text: str, url: str) -> Tuple[str, str]:
    meta = trafilatura.extract_metadata(raw_html, default_url=url) if raw_html else None
    title = (meta.title if meta else "") or _title_from_html(raw_html, extracted_text)
    # trafilatura's sitename falls back to a bare-domain form (stripping a
    # leading "www."/"w1." etc.) when the page has no og:site_name/JSON-LD
    # signal -- that form carries no real information, so detect it by
    # shape (dot-separated labels, no spaces) rather than comparing against
    # the exact original host, which the www-stripping would dodge.
    sitename = (meta.sitename if meta else "") or ""
    if _looks_like_bare_domain(sitename) or not sitename:
        company = _company_from_domain(url)
    else:
        company = _strip_company_boilerplate(sitename)
    return _clean_title(title, company), company


def _job_from_text(url: str, title: str, company: str, description: str, source: str) -> Job:
    return Job(
        title=title,
        company=company,
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
    generator.generate_application(). Writes a vacancy_snapshots row on every
    attempt, successful or not (a thin-content failure still saves the raw
    HTML, in case a human wants to look at it later) — except when neither a
    URL nor pasted text is given, which raises before any attempt is made."""
    url = (url or "").strip()
    paste_text = (paste_text or "").strip()
    if not url and not paste_text:
        raise IngestInputError("A vacancy URL or pasted text is required.")
    store = snapshot_store if snapshot_store is not None else make_snapshot_store()

    if paste_text:
        # A URL is usually given alongside the paste (the error page that asks
        # for pasted text preserves it), so a domain-derived company is
        # normally available even though the text itself wasn't fetched.
        company = _company_from_domain(url) if url else ""
        title = _clean_title(_title_from_html("", paste_text), company)
        job = _job_from_text(url or "pasted-text", title, company, paste_text, "Pasted")
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
            pw_text = trafilatura.extract(pw_html, url=url) or ""
            if len(pw_text) >= MIN_EXTRACTED_CHARS:
                raw_html, extracted = pw_html, pw_text
    if len(extracted) < MIN_EXTRACTED_CHARS:
        store.save(url, job_id="", platform="thin", raw_source=raw_html, extracted_text="")
        raise ThinContentError(url, raw_html)

    title, company = _title_and_company_from_html(raw_html, extracted, url)
    job = _job_from_text(url, title, company, extracted, "Ingested")
    store.save(url, job_id=job.job_id, platform="html",
              raw_source=raw_html, extracted_text=extracted)
    return job.to_record()
