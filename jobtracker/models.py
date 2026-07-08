"""Shared job schema + small HTML/text helpers.

Every adapter must return a list of `Job`. Fields map 1:1 to the storage columns.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, asdict
from typing import Optional

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t]*\n[ \t]*")


def html_to_text(html: Optional[str]) -> str:
    """Very small HTML -> text cleaner (no external deps)."""
    if not html:
        return ""
    text = html
    text = re.sub(r"(?i)<\s*br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</\s*(p|li|div|h[1-6]|ul|ol)\s*>", "\n", text)
    text = re.sub(r"(?i)<\s*li[^>]*>", "• ", text)
    text = _TAG_RE.sub("", text)
    for a, b in (("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"),
                 ("&gt;", ">"), ("&quot;", '"'), ("&#39;", "'"), ("&rsquo;", "'")):
        text = text.replace(a, b)
    text = _WS_RE.sub("\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


@dataclass
class Job:
    title: str
    company: str
    source: str                      # Recruitee / Workday / Teamtailor / ...
    url: str = ""
    location: str = ""
    remote: bool = False
    salary: str = ""
    description: str = ""
    posted_date: str = ""            # ISO date string, best-effort
    job_id: str = ""                 # stable dedup key; auto-filled if empty
    status: str = "New"
    fit_score: Optional[int] = None  # 0-100, set by the LLM scorer (Phase 4)
    fit_reason: str = ""             # one-line rationale from the scorer
    fit_persona: str = ""            # best-matching career path chosen by the scorer

    def __post_init__(self):
        if not self.job_id:
            self.job_id = self.make_id(self.source, self.company, self.title, self.url)

    @staticmethod
    def make_id(*parts: str) -> str:
        raw = "|".join(p.strip().lower() for p in parts if p)
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]

    def to_record(self) -> dict:
        return asdict(self)
