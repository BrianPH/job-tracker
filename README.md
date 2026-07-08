# job-tracker

Scans target companies' careers pages + international remote boards, filters
openings against your rules, scores them with a local LLM, and stores the
survivors in **Postgres** (or local JSON) so you review them in one place.
Includes a **local web UI** with adjustable setpoints, per-vacancy **CV +
motivation letter generation**, and a **daily automatic scrape**.

---

## Quick start

```bash
cd job-tracker
pip install -r requirements.txt

python run_web.py        # web UI + daily scheduler -> http://127.0.0.1:8000
# or headless:
python run.py --dry-run  # fetch + filter, print only
python run.py            # fetch -> rule filter -> LLM score -> store
python show_jobs.py      # browse from the terminal
```

With no database configured everything works against `data/jobs.json`.

Offline tests (no network, no DB needed):

```bash
python -m tests.test_pipeline
python -m tests.test_storage_upsert
python -m tests.test_scorer
python -m tests.test_new_adapters
python -m tests.test_generator
python -m tests.test_webapp
```

---

## The web UI (`python run_web.py`)

- **Vacatureoverzicht** — sorted by LLM fit score, with company, location,
  salary, status and a **link to the original vacancy**. Filter on status,
  source, minimum score, remote-only, or free text.
- **Status editing** — New / Reviewing / Applied / Rejected per job; survives
  re-scrapes.
- **Instellingen (setpoints)** — edit keywords, exclusions, locations,
  **minimum salary**, remote rule, profile text and the daily scrape hour from
  the browser. Saved to `config/profile.yaml`.
- **Scrape nu** — run the whole pipeline in the background; live log at
  `/scrape/log`.
- **CV + brief** — per vacancy, generates a tailored CV and motivation letter
  (.docx) in `applications/<Company>_<Title>/`. Dutch or English is
  auto-detected from the vacancy text.
- **Daily scrape** — APScheduler runs the pipeline once a day at the configured
  hour, as long as the web app is running.

## Access from your phone / other devices

```bash
python run_web.py --lan
```

This binds to all interfaces and prints a `Network: http://<your-ip>:8000`
address — open that on any device on the same wifi. Allow Python through the
Windows Firewall (private networks) when prompted the first time. The app has
no login, so only use `--lan` on networks you trust.

The UI is responsive: on phones the vacancy table becomes stacked cards and
filters/buttons are full-width touch targets.

## CV / motivation-letter generation

Facts (experience, education, skills, contact details) come from
`config/candidate.yaml` and are rendered as-is — the LLM **never invents
facts**. The LLM (same backend as the scorer) only writes the tailored CV
profile paragraph and the letter body, style-guided by your example letters in
`Solliciteren/Motivatie.docx`. Without a reachable LLM a solid fact-based
template is used instead, so generation always works.

Keep `config/candidate.yaml` up to date when your CV changes.

---

## Sources

| Adapter | Companies (enabled) | API |
|---|---|---|
| recruitee | GenomeScan, Leyden Labs | public JSON |
| teamtailor | HUB Organoids | public JSON feed |
| workday | Amgen, Janssen/J&J, Biogen | CXS JSON |
| greenhouse | Adyen, GitLab, Elastic, Mollie | public JSON |
| lever | Zapier, 1Password | public JSON |
| remotive | software-dev + bioinformatics searches | public JSON (remote, international) |
| remoteok | tag-filtered (python/backend/dev) | public JSON (remote, international) |
| academictransfer | LUMC, Erasmus MC, Naturalis | public search API (best-effort) |
| html | BaseClear, The Hyve, Single Cell Discoveries | generic anchor scraper |

Adding a company on an existing platform = one entry in `config/companies.yaml`.
Verify a Greenhouse board via `https://boards-api.greenhouse.io/v1/boards/<board>/jobs`,
a Lever slug via `https://api.lever.co/v0/postings/<slug>?mode=json`. A failing
source only logs an error; the rest of the run continues.

International remote roles are kept by the rule `always_keep_remote: true` even
when they don't match your location list.

---

## Postgres setup (optional but recommended)

```bash
docker run --name jobtracker-db -e POSTGRES_USER=jobtracker \
  -e POSTGRES_PASSWORD=changeme -e POSTGRES_DB=jobtracker \
  -p 5432:5432 -d postgres:16

cp .env.example .env   # set DATABASE_URL accordingly
```

The `jobs` table is created automatically (from `schema.sql`). Upserts preserve
`status`, `fit_score`, `fit_reason` and `first_seen`, so your edits and scores
survive every re-scrape. Verify end-to-end: `python -m tests.test_postgres_integration`.

## LLM fit-scoring + generation — local model, free

```bash
docker compose up -d                                  # Ollama on :11434
docker compose exec ollama ollama pull llama3.2:1b    # ~1.3 GB, tiny + fast
```

`.env` defaults already point at local Ollama. For better letters/scores use a
larger model (`qwen2.5:3b`, `llama3.2:3b`) and set `OLLAMA_MODEL`. Set
`LLM_BACKEND=anthropic` + `ANTHROPIC_API_KEY` for the hosted option.

Scoring is **persona-based**: `config/profile.yaml` defines your career paths
(personas) — e.g. Bioinformatics, .NET Developer, Cloud/DevOps, Data Engineer.
For each vacancy the model picks the single best-matching path and scores the
job against that path alone, so a pure DevOps role is no longer punished for
not being bioinformatics. The chosen persona is stored and shown in the web UI.
`persona_constraints` holds rules that apply to every path (e.g. seniority).
Only new jobs are scored per run. `--rescore` re-scores everything (do this
once after changing personas), `--no-score` skips scoring.

**Test the scoring quickly** with realistic sample vacancies:

```bash
python score_test.py                    # 10 test vacancies vs expected ranges
python score_test.py --only devops      # subset
python score_test.py --model qwen2.5:3b # compare models
python score_test.py --verbose          # show raw model replies
```

Edit `CASES` in `score_test.py` to add your own test vacancies.

---

## Configuration files

- `config/companies.yaml` — sources, per company: `adapter`, `enabled`, `params`.
- `config/profile.yaml` — rules (keywords / excludes / locations /
  `min_salary` / `always_keep_remote`), profile text, `app.scrape_hour`.
  Editable via the web UI.
- `config/candidate.yaml` — structured CV facts for document generation.

## How it fits together

```
config/companies.yaml ─┐
                        ├─► pipeline ─► adapters ─► [Job] ─► rule filter ─► LLM scorer ─► storage
config/profile.yaml ───┘       ▲                                            (Postgres | JSON | Airtable)
                               │
        run.py (CLI) ── run_web.py (FastAPI UI + APScheduler daily run)
                               │
        config/candidate.yaml ─► generator ─► applications/<Company>_<Title>/*.docx
```

- `jobtracker/models.py` — the `Job` schema + HTML cleaner.
- `jobtracker/adapters.py` — one function per platform; returns `list[Job]`.
- `jobtracker/filters.py` — rule filter incl. salary setpoint.
- `jobtracker/scorer.py` — LLM fit-scoring (local Ollama, or Anthropic).
- `jobtracker/generator.py` — CV + motivation letter .docx generation.
- `jobtracker/pipeline.py` — reusable fetch→filter→score→store run.
- `jobtracker/webapp.py` — FastAPI UI, setpoints, scheduler.
- `jobtracker/storage.py` — Postgres / local JSON / Airtable backends.

## Roadmap

- [x] Framework + Recruitee/Teamtailor/Workday adapters
- [x] Postgres storage
- [x] LLM fit-scoring against your profile
- [x] Greenhouse + Lever adapters (software companies)
- [x] Remotive + RemoteOK (international remote)
- [x] AcademicTransfer + generic HTML adapters
- [x] Web UI with links, filters, setpoints (incl. salary)
- [x] CV + motivation letter generation per vacancy
- [x] Daily scheduled scrape (in the web app)
- [ ] E-mail/Slack digest of new high-score matches
- [ ] Playwright-based adapter for JS-rendered career sites
