"""One-time helper: draft config/candidate.yaml from your CV text (issue #9).

Reads a plain-text export of your CV, sends it to the configured LLM backend
(GENERATOR_BACKEND, same as CV/letter generation) with a schema-constrained
prompt, and writes the result as a YAML draft for you to review -- it never
writes to config/candidate.yaml directly. Read the draft, correct anything
wrong or missing, then rename/copy it to config/candidate.yaml yourself.

Usage:
    python import_candidate.py <cv_text_file> [--out config/candidate.draft.yaml]

Exit code is non-zero if no LLM backend is reachable or the reply couldn't be
parsed into the expected shape.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from jobtracker.candidate_import import (
    CandidateImportError,
    draft_candidate_from_text,
    write_candidate_draft,
)
from jobtracker.pipeline import load_env
from jobtracker.scorer import make_caller


def main(argv: list) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("cv_text_file", help="Path to a plain-text export of your CV")
    parser.add_argument("--out", default="config/candidate.draft.yaml",
                        help="Where to write the draft (default: %(default)s)")
    args = parser.parse_args(argv)

    load_env()
    cv_path = Path(args.cv_text_file)
    if not cv_path.exists():
        print(f"No such file: {cv_path}")
        return 2
    cv_text = cv_path.read_text(encoding="utf-8")

    caller = make_caller(json_format=False, temperature=0.2, backend_env="GENERATOR_BACKEND")
    if caller is None:
        print("No LLM backend configured/reachable -- set GENERATOR_BACKEND "
              "(and ANTHROPIC_API_KEY if it's 'anthropic') in .env.")
        return 1

    try:
        data = draft_candidate_from_text(cv_text, caller)
    except CandidateImportError as e:
        print(f"IMPORT FAILED: {e}")
        return 1

    out_path = write_candidate_draft(data, Path(args.out))
    print(f"Draft written to {out_path}")
    print("Review it, fix anything wrong or missing, then rename/copy it to "
         "config/candidate.yaml yourself.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
