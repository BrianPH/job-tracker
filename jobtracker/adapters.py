"""Source adapters. Each returns a list[Job] for one company.

Implemented: recruitee, workday, teamtailor, greenhouse, lever, ashby,
smartrecruiters, remotive, remoteok, adzuna, jooble, academictransfer, html.
The runner dispatches on the `adapter` field in companies.yaml.
"""
from __future__ import annotations

import datetime
import re
from typing import Callable, Dict, List, Optional, Tuple

import requests

from .models import Job, html_to_text

TIMEOUT = 25
HEADERS = {"User-Agent": "job-tracker/0.1 (personal job search)"}
# Workday's CXS endpoint can 403 a bare UA; use a browser-like one.
BROWSER_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Accept": "application/json",
    "Content-Type": "application/json",
}


# --------------------------------------------------------------------------- #
# Recruitee  (GenomeScan, Leyden Labs)                                         #
# Public JSON, no auth: https://<slug>.recruitee.com/api/offers/              #
# --------------------------------------------------------------------------- #
def parse_recruitee_offer(offer: dict, company: str) -> Job:
    sal = offer.get("salary") or {}
    salary = ""
    if sal.get("min") or sal.get("max"):
        cur = sal.get("currency") or ""
        salary = f"{sal.get('min') or ''}-{sal.get('max') or ''} {cur}".strip()

    desc = html_to_text(offer.get("description"))
    reqs = html_to_text(offer.get("requirements"))
    if reqs:
        desc = f"{desc}\n\nRequirements:\n{reqs}".strip()

    return Job(
        title=offer.get("title", "").strip(),
        company=offer.get("company_name") or company,
        source="Recruitee",
        url=offer.get("careers_url") or offer.get("careers_apply_url") or "",
        location=offer.get("location") or offer.get("city") or "",
        remote=bool(offer.get("remote")),
        salary=salary,
        description=desc,
        posted_date=(offer.get("published_at") or offer.get("created_at") or "")[:10],
        job_id=f"recruitee:{offer.get('guid') or offer.get('id')}",
    )


def fetch_recruitee(company: str, params: dict) -> List[Job]:
    slug = params["slug"]
    url = f"https://{slug}.recruitee.com/api/offers/"
    resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    resp.raise_for_status()
    offers = resp.json().get("offers", [])
    return [parse_recruitee_offer(o, company) for o in offers
            if o.get("status", "published") == "published"]


# --------------------------------------------------------------------------- #
# Workday  (Amgen, Biogen, Janssen/J&J)                                        #
# CXS JSON API. List: POST /wday/cxs/<tenant>/<site>/jobs                       #
# Detail:            GET  /wday/cxs/<tenant>/<site><externalPath>               #
# --------------------------------------------------------------------------- #
def parse_workday_posted(text: Optional[str], today: Optional[datetime.date] = None) -> str:
    """Convert Workday's relative 'Posted 5 Days Ago' to an approximate ISO date."""
    if not text:
        return ""
    t = text.lower()
    today = today or datetime.date.today()
    if "today" in t:
        return today.isoformat()
    if "yesterday" in t:
        return (today - datetime.timedelta(days=1)).isoformat()
    m = re.search(r"(\d+)\+?\s*day", t)
    if m:
        return (today - datetime.timedelta(days=int(m.group(1)))).isoformat()
    m = re.search(r"(\d+)\+?\s*month", t)
    if m:
        return (today - datetime.timedelta(days=30 * int(m.group(1)))).isoformat()
    return ""


def _req_id(external_path: str) -> str:
    return external_path.rsplit("/", 1)[-1] if external_path else external_path


def parse_workday_posting(posting: dict, company: str, host: str, site: str, tenant: str) -> Job:
    """Build a Job from one list-level jobPosting (no description yet)."""
    ext = posting.get("externalPath", "")
    public_url = f"https://{host}/{site}{ext}" if ext else f"https://{host}"
    return Job(
        title=(posting.get("title") or "").strip(),
        company=company,
        source="Workday",
        url=public_url,
        location=posting.get("locationsText", "") or "",
        posted_date=parse_workday_posted(posting.get("postedOn")),
        job_id=f"workday:{tenant}:{_req_id(ext)}",
    )


def apply_workday_detail(job: Job, info: dict) -> Job:
    """Merge fields from a jobPostingInfo detail payload into an existing Job."""
    if not info:
        return job
    desc = html_to_text(info.get("jobDescription"))
    if desc:
        job.description = desc
    if info.get("location"):
        job.location = info["location"]
    remote_type = (info.get("remoteType") or "").lower()
    if remote_type:
        job.remote = "remote" in remote_type
    if not job.posted_date:
        start = info.get("startDate") or ""
        if re.match(r"\d{4}-\d{2}-\d{2}", start):
            job.posted_date = start[:10]
    return job


def fetch_workday(company: str, params: dict) -> List[Job]:
    host = params["host"]
    tenant = params.get("tenant") or host.split(".")[0]
    site = params["site"]
    search_text = params.get("search_text", "")
    max_results = int(params.get("max_results", 100))
    want_detail = params.get("descriptions", True)

    base = f"https://{host}/wday/cxs/{tenant}/{site}"
    session = requests.Session()
    session.headers.update(BROWSER_HEADERS)

    jobs: List[Job] = []
    offset, limit, total = 0, 20, None
    while offset < max_results:
        body = {"appliedFacets": {}, "limit": limit, "offset": offset, "searchText": search_text}
        resp = session.post(f"{base}/jobs", json=body, timeout=TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        total = data.get("total", 0)
        postings = data.get("jobPostings", [])
        if not postings:
            break
        for p in postings:
            job = parse_workday_posting(p, company, host, site, tenant)
            if want_detail and p.get("externalPath"):
                try:
                    d = session.get(f"{base}{p['externalPath']}", timeout=TIMEOUT)
                    d.raise_for_status()
                    apply_workday_detail(job, d.json().get("jobPostingInfo", {}))
                except requests.RequestException:
                    pass  # keep the list-level job even if detail fails
            jobs.append(job)
        offset += limit
        if total is not None and offset >= total:
            break
    return jobs


# --------------------------------------------------------------------------- #
# Teamtailor  (HUB Organoids)                                                  #
# Public JSON Feed, no auth: https://<site>/jobs.json  (also /jobs.rss)         #
# --------------------------------------------------------------------------- #
def _teamtailor_location(item: dict) -> str:
    """Teamtailor feeds vary; try common location shapes, else fall back to tags."""
    for key in ("location", "locations"):
        v = item.get(key)
        if isinstance(v, str) and v:
            return v
        if isinstance(v, list) and v:
            return ", ".join(str(x) for x in v)
    tags = item.get("tags")
    if isinstance(tags, list) and tags:
        return ", ".join(str(t) for t in tags)
    return ""


def parse_teamtailor_item(item: dict, company: str) -> Job:
    url = item.get("url") or item.get("id") or ""
    jid = url.rstrip("/").rsplit("/", 1)[-1] if url else (item.get("id") or "")
    desc = item.get("content_text") or html_to_text(item.get("content_html"))
    remote = "remote" in (item.get("title", "") + " " + _teamtailor_location(item)).lower()
    return Job(
        title=(item.get("title") or "").strip(),
        company=company,
        source="Teamtailor",
        url=url,
        location=_teamtailor_location(item),
        remote=remote,
        description=desc,
        posted_date=(item.get("date_published") or "")[:10],
        job_id=f"teamtailor:{jid}",
    )


def fetch_teamtailor(company: str, params: dict) -> List[Job]:
    site = params["site"]
    url = f"https://{site}/jobs.json"
    resp = requests.get(url, headers={"User-Agent": HEADERS["User-Agent"],
                                      "Accept": "application/feed+json, application/json"},
                        timeout=TIMEOUT)
    resp.raise_for_status()
    items = resp.json().get("items", [])
    return [parse_teamtailor_item(it, company) for it in items]


# --------------------------------------------------------------------------- #
# Greenhouse  (many software companies: GitLab, Elastic, Adyen, ...)           #
# Public JSON, no auth: https://boards-api.greenhouse.io/v1/boards/<board>/jobs #
# --------------------------------------------------------------------------- #
def parse_greenhouse_job(item: dict, company: str) -> Job:
    import html as _html
    loc = (item.get("location") or {}).get("name", "") or ""
    offices = item.get("offices") or []
    if not loc and offices:
        loc = ", ".join(o.get("name", "") for o in offices if o.get("name"))
    content = item.get("content") or ""
    desc = html_to_text(_html.unescape(content)) if content else ""
    remote = "remote" in (loc + " " + item.get("title", "")).lower()
    return Job(
        title=(item.get("title") or "").strip(),
        company=company,
        source="Greenhouse",
        url=item.get("absolute_url") or "",
        location=loc,
        remote=remote,
        description=desc,
        posted_date=(item.get("updated_at") or item.get("first_published") or "")[:10],
        job_id=f"greenhouse:{item.get('id')}",
    )


def fetch_greenhouse(company: str, params: dict) -> List[Job]:
    board = params["board"]
    want_content = params.get("descriptions", True)
    url = f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs"
    if want_content:
        url += "?content=true"
    resp = requests.get(url, headers=BROWSER_HEADERS, timeout=TIMEOUT)
    resp.raise_for_status()
    return [parse_greenhouse_job(j, company) for j in resp.json().get("jobs", [])]


# --------------------------------------------------------------------------- #
# Lever  (software companies)                                                  #
# Public JSON, no auth: https://api.lever.co/v0/postings/<slug>?mode=json       #
# --------------------------------------------------------------------------- #
def parse_lever_posting(item: dict, company: str) -> Job:
    cats = item.get("categories") or {}
    loc = cats.get("location") or ""
    if isinstance(item.get("workplaceType"), str):
        remote = item["workplaceType"].lower() == "remote"
    else:
        remote = "remote" in loc.lower()
    salary = ""
    sr = item.get("salaryRange") or {}
    if sr.get("min") or sr.get("max"):
        interval = sr.get("interval", "")
        salary = f"{sr.get('min') or ''}-{sr.get('max') or ''} {sr.get('currency') or ''} {interval}".strip()
    created = item.get("createdAt")
    posted = ""
    if isinstance(created, (int, float)):
        posted = datetime.date.fromtimestamp(created / 1000).isoformat()
    desc = item.get("descriptionPlain") or html_to_text(item.get("description"))
    return Job(
        title=(item.get("text") or "").strip(),
        company=company,
        source="Lever",
        url=item.get("hostedUrl") or "",
        location=loc,
        remote=remote,
        salary=salary,
        description=desc,
        posted_date=posted,
        job_id=f"lever:{item.get('id')}",
    )


def fetch_lever(company: str, params: dict) -> List[Job]:
    slug = params["slug"]
    url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
    resp = requests.get(url, headers=BROWSER_HEADERS, timeout=TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, list):
        return []
    return [parse_lever_posting(it, company) for it in data]


# --------------------------------------------------------------------------- #
# Ashby  (Mollie, Zapier)                                                      #
# Public JSON, no auth: https://api.ashbyhq.com/posting-api/job-board/<token>  #
# --------------------------------------------------------------------------- #
def parse_ashby_job(item: dict, company: str) -> Job:
    loc = item.get("location") or ""
    secondary = [s.get("location", "") for s in (item.get("secondaryLocations") or [])
                 if isinstance(s, dict) and s.get("location")]
    if secondary:
        loc = ", ".join([loc, *secondary]) if loc else ", ".join(secondary)
    workplace = (item.get("workplaceType") or "").lower()
    remote = bool(item.get("isRemote")) or "remote" in workplace
    return Job(
        title=(item.get("title") or "").strip(),
        company=company,
        source="Ashby",
        url=item.get("jobUrl") or item.get("applyUrl") or "",
        location=loc,
        remote=remote,
        description=html_to_text(item.get("descriptionHtml")),
        posted_date=(item.get("publishedAt") or "")[:10],
        job_id=f"ashby:{item.get('id')}",
    )


def fetch_ashby(company: str, params: dict) -> List[Job]:
    token = params["token"]
    url = f"https://api.ashbyhq.com/posting-api/job-board/{token}"
    if params.get("include_compensation"):
        url += "?includeCompensation=true"
    resp = requests.get(url, headers=BROWSER_HEADERS, timeout=TIMEOUT)
    resp.raise_for_status()
    jobs = resp.json().get("jobs", [])
    return [parse_ashby_job(j, company) for j in jobs if j.get("isListed", True)]


# --------------------------------------------------------------------------- #
# SmartRecruiters  (DNAnexus, Genomics England)                                #
# Public JSON, no auth: https://api.smartrecruiters.com/v1/companies/<id>/postings #
# Company identifiers are case-sensitive (e.g. "GenomicsEngland").             #
# --------------------------------------------------------------------------- #
def parse_smartrecruiters_posting(item: dict, company: str) -> Job:
    loc = item.get("location") or {}
    loc_str = loc.get("fullLocation") or ", ".join(
        p for p in (loc.get("city"), loc.get("region"), loc.get("country")) if p)
    posting_id = item.get("id", "")
    identifier = (item.get("company") or {}).get("identifier", "")
    return Job(
        title=(item.get("name") or "").strip(),
        company=(item.get("company") or {}).get("name") or company,
        source="SmartRecruiters",
        url=f"https://jobs.smartrecruiters.com/{identifier}/{posting_id}",
        location=loc_str,
        remote=bool(loc.get("remote")),
        posted_date=(item.get("releasedDate") or "")[:10],
        job_id=f"smartrecruiters:{posting_id}",
    )


def apply_smartrecruiters_detail(job: Job, info: dict) -> Job:
    """Merge fields from a single-posting detail payload into an existing Job."""
    if not info:
        return job
    if info.get("postingUrl"):
        job.url = info["postingUrl"]
    sections = (info.get("jobAd") or {}).get("sections") or {}
    desc = html_to_text((sections.get("jobDescription") or {}).get("text"))
    quals = html_to_text((sections.get("qualifications") or {}).get("text"))
    if quals:
        desc = f"{desc}\n\nQualifications:\n{quals}".strip()
    if desc:
        job.description = desc
    return job


def fetch_smartrecruiters(company: str, params: dict) -> List[Job]:
    company_id = params["company"]
    max_results = int(params.get("max_results", 100))
    want_detail = params.get("descriptions", True)

    base = f"https://api.smartrecruiters.com/v1/companies/{company_id}/postings"
    jobs: List[Job] = []
    offset, limit, total = 0, 100, None
    while offset < max_results:
        resp = requests.get(base, params={"offset": offset, "limit": limit},
                            headers=BROWSER_HEADERS, timeout=TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        total = data.get("totalFound", 0)
        postings = data.get("content", [])
        if not postings:
            break
        for p in postings:
            job = parse_smartrecruiters_posting(p, company)
            if want_detail and p.get("id"):
                try:
                    d = requests.get(f"{base}/{p['id']}", headers=BROWSER_HEADERS, timeout=TIMEOUT)
                    d.raise_for_status()
                    apply_smartrecruiters_detail(job, d.json())
                except requests.RequestException:
                    pass  # keep the list-level job even if detail fails
            jobs.append(job)
        offset += limit
        if total is not None and offset >= total:
            break
    return jobs[:max_results]


# --------------------------------------------------------------------------- #
# Remotive  (international remote board)                                       #
# Public JSON, no auth: https://remotive.com/api/remote-jobs                    #
# --------------------------------------------------------------------------- #
def parse_remotive_job(item: dict, board: str) -> Job:
    return Job(
        title=(item.get("title") or "").strip(),
        company=item.get("company_name") or board,
        source="Remotive",
        url=item.get("url") or "",
        location=item.get("candidate_required_location") or "Remote",
        remote=True,
        salary=item.get("salary") or "",
        description=html_to_text(item.get("description")),
        posted_date=(item.get("publication_date") or "")[:10],
        job_id=f"remotive:{item.get('id')}",
    )


def fetch_remotive(company: str, params: dict) -> List[Job]:
    q = {"limit": str(params.get("limit", 50))}
    if params.get("category"):
        q["category"] = params["category"]
    if params.get("search"):
        q["search"] = params["search"]
    resp = requests.get("https://remotive.com/api/remote-jobs", params=q,
                        headers=BROWSER_HEADERS, timeout=TIMEOUT)
    resp.raise_for_status()
    return [parse_remotive_job(it, company) for it in resp.json().get("jobs", [])]


# --------------------------------------------------------------------------- #
# RemoteOK  (international remote board)                                       #
# Public JSON: https://remoteok.com/api (first element is a legal notice)       #
# --------------------------------------------------------------------------- #
def parse_remoteok_job(item: dict) -> Job:
    salary = ""
    if item.get("salary_min") or item.get("salary_max"):
        salary = f"{item.get('salary_min') or ''}-{item.get('salary_max') or ''} USD".strip()
    return Job(
        title=(item.get("position") or item.get("title") or "").strip(),
        company=item.get("company") or "",
        source="RemoteOK",
        url=item.get("url") or item.get("apply_url") or "",
        location=item.get("location") or "Remote",
        remote=True,
        salary=salary,
        description=html_to_text(item.get("description")),
        posted_date=(item.get("date") or "")[:10],
        job_id=f"remoteok:{item.get('id') or item.get('slug')}",
    )


def fetch_remoteok(company: str, params: dict) -> List[Job]:
    tags = {t.lower() for t in params.get("tags", [])}
    limit = int(params.get("limit", 100))
    resp = requests.get("https://remoteok.com/api", headers=BROWSER_HEADERS, timeout=TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    jobs: List[Job] = []
    for item in data:
        if not isinstance(item, dict) or not (item.get("position") or item.get("title")):
            continue  # skips the legal-notice element
        if tags:
            item_tags = {str(t).lower() for t in (item.get("tags") or [])}
            if not (tags & item_tags):
                continue
        jobs.append(parse_remoteok_job(item))
        if len(jobs) >= limit:
            break
    return jobs


# --------------------------------------------------------------------------- #
# AcademicTransfer  (Dutch academic: LUMC, Erasmus MC, Naturalis)              #
# Public search API (DRF-style, paginated). Parsed defensively — field names   #
# tolerate several shapes since the API is not formally documented.            #
# --------------------------------------------------------------------------- #
def parse_academictransfer_result(item: dict, company: str) -> Job:
    def first(*keys, default=""):
        for k in keys:
            v = item.get(k)
            if v:
                return v
        return default

    url = first("url", "web_url", "detail_url", "absolute_url")
    if url and url.startswith("/"):
        url = f"https://www.academictransfer.com{url}"
    employer = first("employer_name", "organisation_name", "employer") or company
    if isinstance(employer, dict):
        employer = employer.get("name", company)
    loc = first("city", "location", "place")
    return Job(
        title=str(first("title", "job_title", "name")).strip(),
        company=str(employer),
        source="AcademicTransfer",
        url=url,
        location=str(loc),
        description=html_to_text(str(first("description", "abstract", "summary"))),
        posted_date=str(first("publish_date", "start_date", "created"))[:10],
        job_id=f"academictransfer:{first('id', 'slug', default=url)}",
    )


def fetch_academictransfer(company: str, params: dict) -> List[Job]:
    query = params.get("employer") or company
    limit = int(params.get("limit", 50))
    resp = requests.get(
        "https://www.academictransfer.com/api/jobs/",
        params={"search": query, "format": "json", "page_size": str(limit)},
        headers=BROWSER_HEADERS, timeout=TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    results = data.get("results", data) if isinstance(data, dict) else data
    if not isinstance(results, list):
        return []
    return [parse_academictransfer_result(r, company) for r in results if isinstance(r, dict)]


# --------------------------------------------------------------------------- #
# Generic HTML  (small career sites: BaseClear, The Hyve, ...)                 #
# Scrapes anchors from a careers page; optionally fetches each detail page so  #
# the rule filter and LLM scorer have a description to work with.              #
# --------------------------------------------------------------------------- #
_GENERIC_LINK_WORDS = ("vacature", "vacancy", "job", "career", "position", "werken-bij")
# Card-style listings (e.g. "<h3>Role</h3>...<a title='Role'>Read more</a>") often
# give the anchor only a generic call-to-action as text; the real title usually
# lives in the anchor's `title` attribute instead.
_GENERIC_LINK_TEXT = {"read more", "learn more", "more info", "more information",
                       "details", "view", "view job", "view details", "apply",
                       "apply now", "see more", "continue reading",
                       # Dutch equivalents -- several active html-adapter sources
                       # (NKI, Hartwig, UMCG) are Dutch-language.
                       "lees meer", "meer info", "meer informatie", "bekijk",
                       "bekijk vacature", "solliciteer", "solliciteer nu"}


def _absolute_url(base: str, href: str) -> str:
    from urllib.parse import urljoin
    return urljoin(base, href)


def extract_html_links(page_html: str, base_url: str, link_pattern: str = "") -> List[Tuple[str, str]]:
    """Return (title, url) pairs for anchors that look like vacancy links."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(page_html, "html.parser")
    pat = re.compile(link_pattern, re.I) if link_pattern else None
    seen, out = set(), []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        title = " ".join(a.get_text(" ", strip=True).split())
        if title.lower() in _GENERIC_LINK_TEXT:
            title_attr = " ".join((a.get("title") or "").split())
            if title_attr:
                title = title_attr
        if not title or len(title) > 120 or href.startswith(("mailto:", "tel:", "#", "javascript:")):
            continue
        url = _absolute_url(base_url, href)
        if pat:
            keep = bool(pat.search(url) or pat.search(title))
        else:
            hay = (url + " " + title).lower()
            keep = any(w in hay for w in _GENERIC_LINK_WORDS)
        # skip links back to the listing page itself
        if not keep or url.rstrip("/") == base_url.rstrip("/"):
            continue
        if url in seen:
            continue
        seen.add(url)
        out.append((title, url))
    return out


def fetch_html(company: str, params: dict) -> List[Job]:
    careers_url = params["careers_url"]
    if not careers_url:
        return []
    link_pattern = params.get("link_pattern", "")
    want_detail = params.get("descriptions", True)
    max_jobs = int(params.get("max_results", 30))

    resp = requests.get(careers_url, headers=BROWSER_HEADERS, timeout=TIMEOUT)
    resp.raise_for_status()
    links = extract_html_links(resp.text, careers_url, link_pattern)[:max_jobs]

    jobs: List[Job] = []
    for title, url in links:
        desc = ""
        if want_detail:
            try:
                d = requests.get(url, headers=BROWSER_HEADERS, timeout=TIMEOUT)
                d.raise_for_status()
                from bs4 import BeautifulSoup
                soup = BeautifulSoup(d.text, "html.parser")
                for tag in soup(["script", "style", "nav", "header", "footer"]):
                    tag.decompose()
                desc = " ".join(soup.get_text(" ", strip=True).split())[:4000]
            except requests.RequestException:
                pass
        jobs.append(Job(
            title=title, company=company, source="HTML", url=url,
            description=desc, job_id=f"html:{Job.make_id(company, url)}",
        ))
    return jobs



# --------------------------------------------------------------------------- #
# Adzuna  (aggregator: indexes many boards incl. much of what LinkedIn shows)  #
# Official free API: https://developer.adzuna.com — set ADZUNA_APP_ID and      #
# ADZUNA_APP_KEY in .env. Docs: /v1/api/jobs/{country}/search/{page}           #
# --------------------------------------------------------------------------- #
def parse_adzuna_job(item: dict) -> Job:
    comp = item.get("company") or {}
    loc = item.get("location") or {}
    salary = ""
    if item.get("salary_min") or item.get("salary_max"):
        smin = int(item["salary_min"]) if item.get("salary_min") else ""
        smax = int(item["salary_max"]) if item.get("salary_max") else ""
        salary = f"{smin}-{smax} EUR".strip("-")
    title = (item.get("title") or "").replace("<strong>", "").replace("</strong>", "")
    return Job(
        title=title.strip(),
        company=comp.get("display_name") or "",
        source="Adzuna",
        url=item.get("redirect_url") or "",
        location=loc.get("display_name") or "",
        remote="remote" in (title + " " + str(loc.get("display_name") or "")).lower(),
        salary=salary,
        description=html_to_text(item.get("description")),
        posted_date=(item.get("created") or "")[:10],
        job_id=f"adzuna:{item.get('id')}",
    )


def fetch_adzuna(company: str, params: dict) -> List[Job]:
    import os
    app_id = os.getenv("ADZUNA_APP_ID", "")
    app_key = os.getenv("ADZUNA_APP_KEY", "")
    if not app_id or not app_key:
        raise RuntimeError("set ADZUNA_APP_ID and ADZUNA_APP_KEY in .env "
                           "(free key: https://developer.adzuna.com)")
    country = params.get("country", "nl")
    what = params.get("what", "")
    where = params.get("where", "")
    max_results = int(params.get("max_results", 100))
    per_page = 50

    jobs: List[Job] = []
    page = 1
    while len(jobs) < max_results:
        q = {"app_id": app_id, "app_key": app_key,
             "results_per_page": str(per_page), "content-type": "application/json"}
        if what:
            q["what"] = what
        if where:
            q["where"] = where
        resp = requests.get(f"https://api.adzuna.com/v1/api/jobs/{country}/search/{page}",
                            params=q, headers=BROWSER_HEADERS, timeout=TIMEOUT)
        resp.raise_for_status()
        results = resp.json().get("results", [])
        if not results:
            break
        jobs.extend(parse_adzuna_job(r) for r in results)
        if len(results) < per_page:
            break
        page += 1
    return jobs[:max_results]


# --------------------------------------------------------------------------- #
# Jooble  (aggregator, international)                                          #
# Official free API: https://jooble.org/api/about — set JOOBLE_KEY in .env.    #
# POST https://jooble.org/api/<key>  body {"keywords": ..., "location": ...}   #
# --------------------------------------------------------------------------- #
def parse_jooble_job(item: dict) -> Job:
    title = (item.get("title") or "").strip()
    loc = item.get("location") or ""
    return Job(
        title=title,
        company=item.get("company") or "",
        source="Jooble",
        url=item.get("link") or "",
        location=loc,
        remote="remote" in (title + " " + loc + " " + str(item.get("type") or "")).lower(),
        salary=str(item.get("salary") or ""),
        description=html_to_text(item.get("snippet")),
        posted_date=str(item.get("updated") or "")[:10],
        job_id=f"jooble:{item.get('id')}",
    )


def fetch_jooble(company: str, params: dict) -> List[Job]:
    import os
    key = os.getenv("JOOBLE_KEY", "")
    if not key:
        raise RuntimeError("set JOOBLE_KEY in .env (free key: https://jooble.org/api/about)")
    body = {"keywords": params.get("keywords", ""),
            "location": params.get("location", "")}
    if params.get("page"):
        body["page"] = str(params["page"])
    resp = requests.post(f"https://jooble.org/api/{key}", json=body,
                         headers=BROWSER_HEADERS, timeout=TIMEOUT)
    resp.raise_for_status()
    items = resp.json().get("jobs", [])
    limit = int(params.get("limit", 100))
    return [parse_jooble_job(it) for it in items][:limit]


ADAPTERS: Dict[str, Callable[[str, dict], List[Job]]] = {
    "recruitee": fetch_recruitee,
    "workday": fetch_workday,
    "teamtailor": fetch_teamtailor,
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "ashby": fetch_ashby,
    "smartrecruiters": fetch_smartrecruiters,
    "remotive": fetch_remotive,
    "remoteok": fetch_remoteok,
    "adzuna": fetch_adzuna,
    "jooble": fetch_jooble,
    "academictransfer": fetch_academictransfer,
    "html": fetch_html,
}


def get_adapter(name: str) -> Callable[[str, dict], List[Job]]:
    if name not in ADAPTERS:
        raise KeyError(f"Unknown adapter '{name}'. Known: {sorted(ADAPTERS)}")
    return ADAPTERS[name]
