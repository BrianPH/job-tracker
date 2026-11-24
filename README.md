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
python -m tests.test_example_configs
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
| recruitee | GenomeScan, Leyden Labs, ENPICOM | public JSON |
| teamtailor | none currently (HUB Organoids disabled, see below) | public JSON feed |
| workday | Amgen, Janssen/J&J, Biogen, Illumina, Genmab, Wellcome Sanger Institute, EMBL/EMBL-EBI, Velsera, Intellia Therapeutics, CRISPR Therapeutics | CXS JSON |
| greenhouse | Adyen, GitLab, Elastic, Altos Labs, NewLimit, Calico Life Sciences | public JSON |
| lever | 1Password, Retro Biosciences | public JSON |
| ashby | Mollie, Zapier | public JSON |
| smartrecruiters | DNAnexus, Genomics England | public JSON |
| remotive | software-dev + bioinformatics searches | public JSON (remote, international) |
| remoteok | tag-filtered (python/backend/dev) | public JSON (remote, international) |
| adzuna | NL software + bioinformatics searches | official API, free key (aggregator) |
| jooble | NL software + bioinformatics searches | official API, free key (aggregator) |
| academictransfer | none currently (LUMC disabled, see below) | public search API (best-effort) |
| html | The Hyve, NKI, Hartwig Medical Foundation, UMCG/ERIBA; TU Delft, Erasmus MC, Naturalis Biodiversity Center available in `config/companies.example.yaml`*; KNAW, Leiden University also in that file but disabled pending link-pattern verification | generic anchor scraper |

\* Entries in `config/companies.example.yaml` are reference-only -- copy the ones you want into your real `config/companies.yaml` before they're part of any actual run.

Adding a company on an existing platform = one entry in `config/companies.yaml`.
Verify a Greenhouse board via `https://boards-api.greenhouse.io/v1/boards/<board>/jobs`,
a Lever slug via `https://api.lever.co/v0/postings/<slug>?mode=json`, an Ashby
token via `https://api.ashbyhq.com/posting-api/job-board/<token>`, or a
SmartRecruiters company id via
`https://api.smartrecruiters.com/v1/companies/<id>/postings` (identifiers are
case-sensitive). A failing source only logs an error; the rest of the run
continues. Verify a candidate `html` source — and tune its `link_pattern` —
with `python check_source.py <careers_url> [link_pattern]` before adding it to
`companies.yaml`; it fetches the page with the same headers `fetch_html` uses
and reports how many vacancy links the pattern matches.

A university, UMC or research-institute careers page often lists far more
vacancies than the generic adapters elsewhere, and most of them are clinical
or academic rather than software/IT roles. Two extra `html` params filter
*before* the detail-page fetch, so an excluded vacancy never costs a request
and never eats into `max_results`: `title_include` (a list of keywords — only
titles containing at least one get a detail fetch at all) and `title_exclude`
(titles containing any of these are dropped even if `title_include` would
otherwise match — used for PhD/clinical roles). See
`config/companies.example.yaml` for worked examples, including the entries
added for TU Delft, KNAW, Leiden University, Erasmus MC, and Naturalis
Biodiversity Center. That file also records which of the originally-planned
sources in that round (Erasmus University Rotterdam, UvA, VU Amsterdam, UMC
Utrecht, Princess Máxima Center, the eScience Center, IKNL, TNO) could not be
verified yet — their listings are either client-rendered, or the careers URL
used in planning turned out to be wrong.

Mollie and Zapier moved from Greenhouse/Lever to Ashby. HUB Organoids
(Teamtailor, custom domain) and BaseClear and Single Cell Discoveries (both
`html`) are disabled in `config/companies.yaml`, each with a comment that
explains the reason: their source sites return genuine errors, or their page
structure cannot be scraped by the generic adapters. A URL change alone does
not fix these. LUMC is disabled for the same underlying reason — its listing
is rendered client-side — and stays disabled until the planned
Playwright-based adapter lands; Erasmus MC and Naturalis Biodiversity Center
used to be attempted through `academictransfer` as well, but now have working
direct `html` entries instead (see `config/companies.example.yaml`).

International remote roles are kept by the rule `always_keep_remote: true` even
when they don't match your location list.

**Aggregators (LinkedIn-adjacent coverage):** Adzuna and Jooble index most major
job boards, including much of what LinkedIn shows. Both need a free API key in
`.env` (`ADZUNA_APP_ID`/`ADZUNA_APP_KEY` from developer.adzuna.com, `JOOBLE_KEY`
from jooble.org/api/about); until keys are set those sources just log an error
and the rest of the run continues. Add more searches or countries by copying the
entries in `companies.yaml` (Adzuna: `country: gb`, `us`, etc.).

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

The stack no longer runs its own Ollama container. It uses the Ollama
instance installed on the Pi host (shared with CHESHIRE), reached from the
app container via `host.docker.internal:11434`. One-time host setup:

```bash
ollama pull qwen2.5:3b        # shared scoring/generation model (~1.9 GB)
# free RAM quickly after each request:
sudo systemctl edit ollama    # add: Environment="OLLAMA_KEEP_ALIVE=30s"
sudo systemctl restart ollama
```

The compose file defaults to `qwen2.5:3b`; override with `OLLAMA_MODEL` in
`.env` if you want a different model. When running outside Docker
(`python run_web.py` on the host), the default `http://localhost:11434`
reaches the same instance directly.

Scoring and CV/letter generation each select their backend independently, through
two separate environment variables: `LLM_BACKEND` controls scoring, and
`GENERATOR_BACKEND` controls generation. Both default to `ollama`, so setting one
of them to `anthropic` does not change the other. Set either variable to
`anthropic`, and set `ANTHROPIC_API_KEY`, to use the hosted option for that part
of the pipeline. Generation needs long, complete replies, so it uses a separate
model and a separate token budget from scoring's short JSON replies:
`ANTHROPIC_MODEL_EXTRACT` (default `claude-haiku-4-5-20251001`) for scoring, and
`ANTHROPIC_MODEL_WRITE` (default `claude-sonnet-5`) for generation. A reply
that gets cut off by the token limit is treated as a failed generation, not
returned to the caller truncated.

Scoring is **persona-based**: `config/profile.yaml` defines your career paths
(personas) — e.g. Bioinformatics, .NET Developer, Cloud/DevOps, Data Engineer.
For each vacancy the model picks the single best-matching path and scores the
job against that path alone, so a pure DevOps role is no longer punished for
not being bioinformatics. The chosen persona is stored and shown in the web UI.
`persona_constraints` holds rules that apply to every path (e.g. seniority).
Only new jobs are scored per run; existing scores are preserved on re-scrapes
by design — so **changing the model or personas does not update old scores
automatically**. Use the **"Herscoor alles"** button in the web UI (or
`python run.py --rescore`) after switching `OLLAMA_MODEL` or editing personas.
`--no-score` skips scoring.

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

All three files above hold personal data and are git-ignored. If
`config/profile.yaml` is missing, the pipeline and generator fall back to
`config/profile.example.yaml`, which is tracked in the repo — copy it to
`config/profile.yaml` and fill in your own details. `config/companies.yaml`
has no such runtime fallback; `config/companies.example.yaml` is only a
reference list of entries to copy in by hand (see the Sources section
above).

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
- `check_source.py` — stand-alone CLI to verify a candidate `html` source
  (and tune its `link_pattern`) before adding it to `companies.yaml`.

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
