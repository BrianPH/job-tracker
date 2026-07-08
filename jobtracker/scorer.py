"""Phase 4: LLM fit-scoring with a LOCAL model (free, private).

Scores each job 0-100 against your profile and attaches a one-line reason, so the
review list ranks by relevance instead of keyword matches.

Default backend is a local Ollama server (run in Docker — see docker-compose.yml).
No API keys, no per-call cost. An Anthropic backend is available as an option.

Small local models are unreliable at emitting a big JSON array for many jobs, so we
score ONE job per request and ask for a tiny object {"score":int,"reason":str} — the
mapping back to the job is then trivial. Set JOBTRACKER_DEBUG=1 to print raw replies.
"""
from __future__ import annotations

import json
import os
import re
from typing import Callable, List, Optional, Tuple

import requests

# --- backends -------------------------------------------------------------- #
OLLAMA_URL = "http://localhost:11434"
OLLAMA_MODEL = "llama3.2:1b"          # tiny + fast; try qwen2.5:3b / llama3.2:3b for accuracy
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_MODEL = "claude-3-5-haiku-latest"

DESC_CHARS = 900

SYSTEM = (
    "You rate how well ONE job matches ONE candidate, as a JSON object. Judge ONLY the "
    "job's actual responsibilities against the candidate's field and skills.\n"
    "Rules:\n"
    "- A job in an unrelated field (manufacturing/production operator, warehouse, sales, "
    "general management, HR, finance, occupational safety, facilities) is a POOR match for "
    "a bioinformatics/software/data candidate and MUST score below 30 — even at a "
    "biotech/pharma company.\n"
    "- Score 70+ ONLY if the job's core work IS the candidate's field (bioinformatics, "
    "computational biology, data science/engineering, software, NGS pipelines).\n"
    "- The \"reason\" must describe what the JOB actually involves and why it does or does "
    "not match. Do NOT restate the candidate's skills.\n"
    "Example: candidate is a bioinformatician. Job 'Manufacturing Operator' -> "
    '{"score": 8, "reason": "Production-line operator role; no computational or bioinformatics work."} '
    "Job 'Bioinformatics Scientist, NGS pipelines' -> "
    '{"score": 90, "reason": "Core duties are NGS pipeline development, directly matching the candidate."}\n'
    'Output ONLY a JSON object: {"score": <integer 0-100>, "reason": "<one sentence about the job>"}.'
)

# A caller takes (system, user) and returns the model's raw text reply.
Caller = Callable[[str, str], str]


def _profile_text(profile: dict) -> str:
    p = (profile or {}).get("profile", {}) or {}
    parts = [f"Candidate summary: {p.get('summary', '').strip() or '(not provided)'}"]
    if p.get("must_haves"):
        parts.append("Must-haves: " + ", ".join(map(str, p["must_haves"])))
    if p.get("nice_to_haves"):
        parts.append("Nice-to-haves: " + ", ".join(map(str, p["nice_to_haves"])))
    rules = (profile or {}).get("rules", {}) or {}
    if rules.get("keywords_any"):
        parts.append("Target areas: " + ", ".join(map(str, rules["keywords_any"])))
    return "\n".join(parts)


def build_job_prompt(profile: dict, job) -> Tuple[str, str]:
    desc = (job.description or "").strip().replace("\n", " ")[:DESC_CHARS]
    user = (
        "CANDIDATE (the person applying):\n"
        f"{_profile_text(profile)}\n\n"
        "JOB (rate how well THIS job matches the candidate above):\n"
        f"Title: {job.title}\n"
        f"Company: {job.company}\n"
        f"Location: {job.location}\n"
        f"Description: {desc or '(no description available)'}\n\n"
        'Output ONLY {"score": <0-100>, "reason": "<one sentence about the job>"}.'
    )
    return SYSTEM, user


def parse_score(text: str) -> Optional[Tuple[int, str]]:
    """Read {"score":int,"reason":str} from a reply. Tolerates arrays / extra prose."""
    if not text:
        return None
    obj = None
    try:
        data = json.loads(text.strip())
        obj = data[0] if isinstance(data, list) and data else data
    except (json.JSONDecodeError, TypeError):
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            try:
                obj = json.loads(m.group(0))
            except json.JSONDecodeError:
                obj = None
    if not isinstance(obj, dict):
        return None
    if "score" not in obj:  # sometimes wrapped, e.g. {"result": {...}}
        for v in obj.values():
            if isinstance(v, dict) and "score" in v:
                obj = v
                break
    try:
        score = int(round(float(obj.get("score"))))
    except (TypeError, ValueError):
        return None
    return max(0, min(100, score)), str(obj.get("reason", "")).strip()


def ollama_caller(base_url: str = OLLAMA_URL, model: str = OLLAMA_MODEL,
                  json_format: bool = True, temperature: float = 0) -> Caller:
    def _call(system: str, user: str) -> str:
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": False,
            "options": {"temperature": temperature},
        }
        if json_format:
            payload["format"] = "json"
        resp = requests.post(
            f"{base_url.rstrip('/')}/api/chat",
            json=payload,
            timeout=300,
        )
        resp.raise_for_status()
        return resp.json().get("message", {}).get("content", "")
    return _call


def anthropic_caller(api_key: str, model: str = ANTHROPIC_MODEL) -> Caller:
    def _call(system: str, user: str) -> str:
        resp = requests.post(
            ANTHROPIC_URL,
            headers={"x-api-key": api_key, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": model, "max_tokens": 512, "system": system,
                  "messages": [{"role": "user", "content": user}]},
            timeout=60,
        )
        resp.raise_for_status()
        blocks = resp.json().get("content", [])
        return "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
    return _call


def make_caller(json_format: bool = True, temperature: float = 0) -> Optional[Caller]:
    """Pick a backend from env. Default: local Ollama.

    json_format=False returns a plain-text caller (used by the CV/letter generator).
    """
    backend = os.getenv("LLM_BACKEND", "ollama").lower()
    if backend == "ollama":
        return ollama_caller(os.getenv("OLLAMA_HOST", OLLAMA_URL),
                             os.getenv("OLLAMA_MODEL", OLLAMA_MODEL),
                             json_format=json_format, temperature=temperature)
    if backend == "anthropic":
        key = os.getenv("ANTHROPIC_API_KEY")
        return anthropic_caller(key, os.getenv("ANTHROPIC_MODEL", ANTHROPIC_MODEL)) if key else None
    return None


def score_jobs(jobs: List, profile: dict, caller: Optional[Caller] = None) -> int:
    """Score jobs in place (one request per job); returns how many got a score."""
    if not jobs:
        return 0
    if caller is None:
        caller = make_caller()
    if caller is None:
        return 0

    debug = bool(os.getenv("JOBTRACKER_DEBUG"))
    scored = 0
    for job in jobs:
        system, user = build_job_prompt(profile, job)
        try:
            text = caller(system, user)
        except Exception as e:  # noqa: BLE001 - one bad job shouldn't kill the run
            if debug:
                print(f"  [debug] call failed for {job.job_id}: {e}")
            continue
        result = parse_score(text)
        if result is None:
            if debug:
                print(f"  [debug] unparseable reply for {job.job_id}: {text[:200]!r}")
            continue
        job.fit_score, job.fit_reason = result
        scored += 1
    return scored
