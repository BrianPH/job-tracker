#!/usr/bin/env python3
"""Browse stored jobs, ranked by fit score.

Usage:
    python show_jobs.py                     # all jobs, best fit first
    python show_jobs.py --status New        # only New
    python show_jobs.py --company Genome     # filter by company (substring)
    python show_jobs.py --min-score 70       # only strong matches
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from jobtracker.storage import make_store

ROOT = Path(__file__).parent


def load_env(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--status")
    ap.add_argument("--company")
    ap.add_argument("--min-score", type=int)
    args = ap.parse_args()

    load_env(ROOT / ".env")
    store, backend = make_store()
    if not hasattr(store, "fetch"):
        print(f"Backend '{backend}' has no fetch(); browse it in its own UI.")
        return 1

    rows = store.fetch(status=args.status, company=args.company, min_score=args.min_score)
    if not rows:
        print("No jobs found.")
        return 0

    print(f"{len(rows)} job(s) from {backend} (best fit first):\n")
    for r in rows:
        score = r.get("fit_score")
        badge = f"{score:>3}" if score is not None else "  –"
        remote = " [remote]" if r.get("remote") else ""
        posted = r.get("posted_date") or "?"
        print(f"  ({badge}) [{r.get('status','New'):9s}] {r.get('company','')} — {r.get('title','')}")
        print(f"          {r.get('location','')}{remote}  ·  posted {posted}")
        if r.get("fit_reason"):
            print(f"          fit: {r['fit_reason']}")
        if r.get("url"):
            print(f"          {r['url']}")
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
