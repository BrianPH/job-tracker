"""Phase 4: LLM fit-scoring with a LOCAL model (free, private).

Persona-based: the candidate has SEVERAL acceptable career paths (personas in
config/profile.yaml). For each job the model picks the SINGLE best-matching
persona and scores the job against that path alone — a vacancy does not need
to match every skill the candidate has. The chosen persona is stored on the
job (fit_persona) and shown in the web UI.

Default backend is a local Ollama server (docker-compose.yml). Small local
models are unreliable at big JSON arrays, so we score ONE job per request and
ask for {"persona": str, "score": int, "reason": str}.
Set JOBTRACKER_DEBUG=1 to print raw replies.

Quick manual check of scoring quality:  python score_test.py
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

# Fallback personas if config/profile.yaml has none (keeps old profiles working).
DEFAULT_PERSONAS = [
    {"name": "Generalist", "summary": "See candidate summary."},
]

SYSTEM = (
    "You rate how well ONE job matches ONE candidate. The candidate is open to SEVERAL "
    "career paths (personas). Your task:\n"
    "1. Pick the ONE persona that best matches this job.\n"
    "2. Score 0-100 how well the job fits THAT persona alone. IMPORTANT: the job only "
    "needs to fit ONE path well to score high — never lower a score because the job "
    "doesn't cover the candidate's other paths or other skills.\n"
    "Scoring guide:\n"
    "- 70-100: the job's core work is what the chosen persona does.\n"
    "- 40-69: substantial overlap with the chosen persona, but a notable gap "
    "(e.g. different stack, or heavy seniority requirements).\n"
    "- 0-30: no persona fits — non-technical work (production/manufacturing operator, "
    "warehouse, sales, HR, finance, care, facilities), even at an attractive company.\n"
    "Also apply the candidate constraints (seniority etc.) given in the prompt.\n"
    "The \"reason\" must describe what the JOB involves and why it fits the chosen "
    "persona or not. Do NOT list the candidate's other skills.\n"
    "Example: personas are 'Bioinformatics' and 'Cloud / DevOps Engineer'. "
    "Job 'CI/CD Engineer' -> "
    '{"persona": "Cloud / DevOps Engineer", "score": 85, "reason": "Core work is building '
    'CI/CD pipelines, exactly the DevOps path; bioinformatics is irrelevant here."}\n'
    "Job 'Manufacturing Operator' -> "
    '{"persona": "none", "score": 8, "reason": "Production-line work; matches no technical path."}\n'
    'Output ONLY a JSON object: {"persona": "<persona name or none>", '
    '"score": <integer 0-100>, "reason": "<one sentence about the job>"}.'
)

# A caller takes (system, user) and returns the model's raw text reply.
Caller = Callable[[str, str], str]


def get_personas(profile: dict) -> list:
    personas = (profile or {}).get("personas") or []
    out = []
    for p in personas:
        if isinstance(p, dict) and p.get("name"):
            out.append({"name": str(p["name"]).strip(),
                        "summary": " ".join(str(p.get("summary", "")).split())})
    return out or DEFAULT_PERSONAS


def _personas_text(profile: dict) -> str:
    lines = []
    for i, p in enumerate(get_personas(profile), 1):
        lines.append(f"{i}. {p['name']}: {p['summary']}")
    return "\n".join(lines)


def _constraints_text(profile: dict) -> str:
    c = " ".join(str((profile or {}).get("persona_constraints", "")).split())
    if not c:
        p = (profile or {}).get("profile", {}) or {}
        c = " ".join(str(p.get("summary", "")).split())[:400]
    return c or "(none)"


def build_job_prompt(profile: dict, job) -> Tuple[str, str]:
    desc = (job.description or "").strip().replace("\n", " ")[:DESC_CHARS]
    user = (
        "CANDIDATE career paths (personas) — the job needs to fit only ONE of these:\n"
        f"{_personas_text(profile)}\n\n"
        f"CANDIDATE constraints: {_constraints_text(profile)}\n\n"
        "JOB (pick the best persona, then rate the job against that persona only):\n"
        f"Title: {job.title}\n"
        f"Company: {job.company}\n"
        f"Location: {job.location}\n"
        f"Description: {desc or '(no description available)'}\n\n"
        'Output ONLY {"persona": "<name or none>", "score": <0-100>, '
        '"reason": "<one sentence about the job>"}.'
    )
    return SYSTEM, user


def parse_score(text: str) -> Optional[Tuple[int, str, str]]:
    """Read {"persona","score","reason"} from a reply -> (score, reason, persona).

    Tolerates arrays, wrapped objects, extra prose, and a missing persona field.
    """
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
    persona = str(obj.get("persona", "") or "").strip()
    if persona.lower() in ("none", "null", "-", "n/a"):
        persona = ""
    return max(0, min(100, score)), str(obj.get("reason", "")).strip(), persona


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
        job.fit_score, job.fit_reason, job.fit_persona = result
        scored += 1
    return scored
