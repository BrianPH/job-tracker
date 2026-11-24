#!/usr/bin/env python3
"""Quick manual check of how the LLM interprets and scores vacancies.

Runs a fixed set of realistic test vacancies through the REAL configured LLM
(local Ollama by default — make sure it is running) and prints, per vacancy:
the chosen persona, the score, the reason, and whether the score falls in the
expected range.

Usage:
    python score_test.py                  # all cases, configured model
    python score_test.py --only devops    # cases whose title matches "devops"
    python score_test.py --model qwen2.5:3b   # try another Ollama model
    python score_test.py --fake           # no LLM: plumbing check only
    python score_test.py --verbose        # also print raw model replies

Edit CASES below to add your own test vacancies. Expected ranges are wide on
purpose: small local models are noisy. Many misses in a row usually means the
model is too small — try qwen2.5:3b or llama3.2:3b.
"""
from __future__ import annotations

import argparse
import os
import sys

from jobtracker.models import Job
from jobtracker.pipeline import load_env, load_profile
from jobtracker.scorer import build_job_prompt, make_caller, parse_score

# (job, expected_min, expected_max) — ranges are deliberately generous.
CASES = [
    (Job(title="Bioinformatics Scientist (NGS pipelines)", company="GenomeScan", source="test",
         location="Leiden",
         description="Develop and maintain NGS data analysis pipelines in Python and Nextflow. "
                     "Process sequencing data, build QC tooling, work with Linux and Docker on our "
                     "compute cluster. Junior scientists are encouraged to apply."),
     70, 100),
    (Job(title="Senior CI/CD Engineer", company="PayFlow (fintech)", source="test",
         location="Amsterdam",
         description="Own our CI/CD platform: Azure DevOps pipelines, Docker, Kubernetes, "
                     "infrastructure automation and developer tooling for a payments platform. "
                     "You bring solid pipeline and Linux experience."),
     50, 95),
    (Job(title=".NET Software Developer", company="LogiSoft", source="test",
         location="Rotterdam",
         description="Build backend services and web applications in C# / ASP.NET Core with "
                     "SQL Server and Azure. You will design APIs and work in a scrum team. "
                     "1-3 years experience or strong graduates welcome."),
     70, 100),
    (Job(title="Junior Python Backend Developer", company="CloudWorks", source="test",
         location="Remote", remote=True,
         description="Develop REST APIs in Python (FastAPI), PostgreSQL, Docker. Fully remote "
                     "team. Great first or second job for a strong junior."),
     70, 100),
    (Job(title="Data Engineer", company="RetailData", source="test",
         location="Utrecht",
         description="Build and operate data pipelines (Python, SQL, dbt) and cloud data "
                     "warehouses. You automate ingestion and transformations."),
     60, 100),
    (Job(title="Manufacturing Operator", company="Biogen", source="test",
         location="Leiden",
         description="Operate and monitor production equipment for biologics manufacturing. "
                     "Follow GMP procedures, execute batch records, clean rooms."),
     0, 30),
    (Job(title="Sales Manager SaaS", company="CloudCRM", source="test",
         location="Amsterdam",
         description="Drive new business, manage the sales cycle, hit quota, build pipeline of "
                     "enterprise customers for our CRM platform."),
     0, 25),
    (Job(title="Verpleegkundige", company="Erasmus MC", source="test",
         location="Rotterdam",
         description="Als verpleegkundige lever je directe patiëntenzorg op de afdeling "
                     "cardiologie. Je werkt in wisselende diensten."),
     0, 20),
    (Job(title="Staff Software Engineer (10+ years)", company="ScaleUp", source="test",
         location="Amsterdam",
         description="Technical leadership across teams, architecture of distributed systems, "
                     "mentoring seniors. Minimum 10 years of production engineering experience."),
     5, 55),
    (Job(title="Bioinformatician", company="LUMC", source="test",
         location="Leiden",
         description="Support genetics researchers with analysis of sequencing data, variant "
                     "calling workflows, and development of analysis scripts in Python and R."),
     65, 100),
]


def fake_caller(system: str, user: str) -> str:
    return '{"persona": "Software Developer (.NET / backend)", "score": 75, "reason": "fake"}'


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="", help="run only cases whose title contains this text")
    ap.add_argument("--model", default="", help="override OLLAMA_MODEL for this run")
    ap.add_argument("--fake", action="store_true", help="use a fake LLM (plumbing check)")
    ap.add_argument("--verbose", action="store_true", help="print raw model replies")
    args = ap.parse_args()

    load_env()
    if args.model:
        os.environ["OLLAMA_MODEL"] = args.model
    profile = load_profile()

    caller = fake_caller if args.fake else make_caller()
    if caller is None:
        print("No LLM backend configured (set LLM_BACKEND in .env, or use --fake).")
        return 1
    if not args.fake:
        backend = os.getenv("LLM_BACKEND", "ollama")
        model = os.getenv("OLLAMA_MODEL", "llama3.2:1b") if backend == "ollama" \
            else os.getenv("ANTHROPIC_MODEL_EXTRACT", "claude-haiku-4-5-20251001")
        print(f"Backend: {backend} ({model})\n")

    cases = [(j, lo, hi) for j, lo, hi in CASES
             if args.only.lower() in j.title.lower()]
    if not cases:
        print(f"No cases match --only {args.only!r}")
        return 1

    hits = 0
    for job, lo, hi in cases:
        system, user = build_job_prompt(profile, job)
        try:
            reply = caller(system, user)
        except Exception as e:  # noqa: BLE001
            print(f"✗ ERROR    {job.title}: {e}")
            print("  (Is Ollama running? docker compose up -d)")
            return 1
        result = parse_score(reply)
        if args.verbose:
            print(f"  raw: {reply[:300]!r}")
        if result is None:
            print(f"✗ UNPARSEABLE  {job.title}  reply: {reply[:120]!r}")
            continue
        score, reason, persona = result
        ok = lo <= score <= hi
        hits += ok
        mark = "✓" if ok else "✗"
        print(f"{mark} {score:3d}  (expected {lo:3d}-{hi:3d})  {job.title}  [{persona or 'geen persona'}]")
        print(f"      {reason}")
    print(f"\n{hits}/{len(cases)} within expected range.")
    if hits < len(cases):
        print("Tip: misses are normal with llama3.2:1b; try a larger model:")
        print("  docker compose exec ollama ollama pull qwen2.5:3b")
        print("  python score_test.py --model qwen2.5:3b")
    return 0 if hits == len(cases) else 2


if __name__ == "__main__":
    sys.exit(main())
