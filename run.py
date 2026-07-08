#!/usr/bin/env python3
"""Run the pipeline: fetch enabled companies -> rule filter -> LLM score -> store.

Usage:
    python run.py                 # fetch, filter, score new jobs, store
    python run.py --no-filter     # skip rule filter (store everything)
    python run.py --no-score      # skip LLM scoring
    python run.py --rescore       # re-score all kept jobs
    python run.py --dry-run       # fetch + filter, print, don't score or store

For the web interface + daily scheduler, use: python run_web.py
"""
from __future__ import annotations

import argparse
import sys

from jobtracker.pipeline import run_pipeline


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-filter", action="store_true", help="store everything, skip rule filter")
    ap.add_argument("--no-score", action="store_true", help="skip LLM fit-scoring")
    ap.add_argument("--rescore", action="store_true", help="re-score all kept jobs, even if already scored")
    ap.add_argument("--dry-run", action="store_true", help="don't score or write to storage")
    args = ap.parse_args()

    summary = run_pipeline(no_filter=args.no_filter, no_score=args.no_score,
                           rescore=args.rescore, dry_run=args.dry_run)
    return 0 if summary["companies"] else 1


if __name__ == "__main__":
    sys.exit(main())
