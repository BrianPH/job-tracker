"""Cheap rule-based filtering (Phase 2). Runs before any LLM scoring."""
from __future__ import annotations

import re
from typing import List, Optional

from .models import Job

_NUM_RE = re.compile(r"(\d[\d.,]*)\s*(k)?", re.I)


def parse_salary_max(salary: str) -> Optional[float]:
    """Best-effort: highest yearly amount in a free-text salary string, or None.

    Handles "50000-60000 EUR", "€3.500 - €4.500 per month", "90k-120k USD".
    Monthly amounts are multiplied by 12. Currency is ignored (setpoint is a
    rough floor, most postings are EUR/USD).
    """
    if not salary:
        return None
    amounts = []
    for num, k in _NUM_RE.findall(salary):
        cleaned = num.replace(".", "").replace(",", "")
        if not cleaned.isdigit():
            continue
        val = float(cleaned)
        if k:
            val *= 1000
        if val >= 100:  # ignore stray small numbers like "40 hours"
            amounts.append(val)
    if not amounts:
        return None
    best = max(amounts)
    if re.search(r"(?i)month|maand|p/?m\b", salary) or best < 10000:
        best *= 12
    return best


def _text(job: Job) -> str:
    return f"{job.title}\n{job.description}".lower()


def passes(job: Job, rules: dict) -> bool:
    rules = rules or {}
    text = _text(job)
    loc = job.location.lower()

    exclude = [w.lower() for w in rules.get("exclude_any", []) if w]
    if any(w in text for w in exclude):
        return False

    keywords = [w.lower() for w in rules.get("keywords_any", []) if w]
    if keywords and not any(w in text for w in keywords):
        return False

    # Salary floor: only drops jobs that STATE a salary below the setpoint.
    # Jobs without salary info always pass (most postings omit it).
    min_salary = rules.get("min_salary") or 0
    if min_salary:
        stated = parse_salary_max(job.salary)
        if stated is not None and stated < float(min_salary):
            return False

    if rules.get("always_keep_remote", True) and job.remote:
        return True

    locations = [w.lower() for w in rules.get("locations_any", []) if w]
    if locations and not any(w in loc for w in locations):
        return False

    return True


def apply_rules(jobs: List[Job], profile: dict) -> List[Job]:
    rules = (profile or {}).get("rules", {})
    return [j for j in jobs if passes(j, rules)]
