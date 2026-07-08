"""Reusable pipeline: fetch enabled companies -> rule filter -> LLM score -> store.

Used by run.py (CLI), the webapp's scrape button, and the daily scheduler.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Callable, List, Optional

import yaml

from .adapters import get_adapter
from .filters import apply_rules
from .storage import make_store

ROOT = Path(__file__).resolve().parents[1]


def load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def load_env(path: Optional[Path] = None) -> None:
    path = path or (ROOT / ".env")
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


def load_profile() -> dict:
    return (load_yaml(ROOT / "config" / "profile.yaml")
            or load_yaml(ROOT / "config" / "profile.example.yaml"))


def run_pipeline(no_filter: bool = False, no_score: bool = False, rescore: bool = False,
                 dry_run: bool = False,
                 log: Callable[[str], None] = print) -> dict:
    """Run the full pipeline; returns a summary dict.

    log() receives progress lines (print for the CLI, a collector for the webapp).
    """
    load_env()
    companies = load_yaml(ROOT / "config" / "companies.yaml").get("companies", [])
    profile = load_profile()

    enabled = [c for c in companies if c.get("enabled")]
    summary = {"companies": len(enabled), "fetched": 0, "kept": 0,
               "scored": 0, "errors": [], "store": None, "result": None}
    if not enabled:
        log("No enabled companies in config/companies.yaml")
        return summary

    all_jobs: List = []
    for c in enabled:
        name, adapter_name, params = c["name"], c["adapter"], c.get("params", {})
        try:
            jobs = get_adapter(adapter_name)(name, params)
            log(f"  {name:32s} [{adapter_name}] -> {len(jobs)} openings")
            all_jobs.extend(jobs)
        except Exception as e:  # noqa: BLE001 - one source must not kill the run
            msg = f"{name} [{adapter_name}]: {e}"
            summary["errors"].append(msg)
            log(f"  {name:32s} [{adapter_name}] -> ERROR: {e}")

    summary["fetched"] = len(all_jobs)
    log(f"Fetched {len(all_jobs)} openings total.")

    if not no_filter:
        kept = apply_rules(all_jobs, profile)
        log(f"Rule filter: {len(kept)}/{len(all_jobs)} kept.")
        all_jobs = kept
    summary["kept"] = len(all_jobs)

    if dry_run:
        for j in all_jobs:
            log(f"  - {j.company}: {j.title} ({j.location}){' [remote]' if j.remote else ''}")
        log("(dry run — nothing scored or written)")
        return summary

    store, backend = make_store()
    summary["store"] = backend

    if not no_score:
        from .scorer import score_jobs, make_caller
        if make_caller() is None:
            log("(No LLM backend configured — skipping scoring. Set LLM_BACKEND in .env.)")
        else:
            already = set() if rescore else (store.scored_ids() if hasattr(store, "scored_ids") else set())
            to_score = [j for j in all_jobs if j.job_id not in already]
            n = score_jobs(to_score, profile)
            summary["scored"] = n
            log(f"LLM scoring: scored {n}/{len(to_score)} new jobs.")

    result = store.upsert(all_jobs)
    summary["result"] = result
    log(f"Stored via {backend}: {result}")
    return summary
