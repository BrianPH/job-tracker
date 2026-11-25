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
python -m tests.test_werkenvoornederland
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
  the browser. Saved to `config/profile.yaml`. Keyword/exclusion matching is
  left-anchored, not plain substring: a keyword matches as a whole word or as
  the start of a longer word (so a short keyword like `arts` no longer
  wrongly matches inside `starts`/`parts`, and a deliberately short keyword
  like `bioinformatic` still matches `bioinformatics`/`bioinformatician`),
  but it will NOT match a keyword glued onto the END of a longer word (e.g.
  `arts` alone does not match the compound `huisarts`) — list such compounds
  as their own keyword instead.
- **Scrape nu** — run the whole pipeline in the background; live log at
  `/scrape/log`.
- **CV + brief** — per vacancy, generates a tailored CV and motivation letter
  (.docx) in `applications/<Company>_<Title>/`. Dutch or English is
  auto-detected from the vacancy text.
- **Vanaf URL** (`/ingest`) — generate a CV + brief for a vacancy that was
  never scraped: paste its URL (or the vacancy text itself) and a tailored
  CV/letter is generated the same way as for a scraped job. See
  [Generating from a bare URL](#generating-from-a-bare-url) below.
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

Keep `config/candidate.yaml` up to date when your CV changes. It isn't
tracked (git-ignored, like the other personal config files); copy
`config/candidate.example.yaml` to `config/candidate.yaml` and fill in your
own details. See that file's own comments for the full schema — in short,
each `experience` entry has a stable `id` and a **bullet bank**: a list of
`bullets`, each with its own `id`, free-form `tags`, and `nl`/`en` text. A
future tailoring step (picking/ordering bullets per vacancy) will reference
these ids; it will only ever select existing bullet text, never write new
bullet text, so a tailored CV stays verifiable against these facts.

### Document layout: docxtpl templates

The CV and letter `.docx` files are rendered from
[`docxtpl`](https://docxtpl.readthedocs.io/) templates in
`config/templates/` (`cv_nl.docx`, `cv_en.docx`, `letter_nl.docx`,
`letter_en.docx`) — Word documents with Jinja placeholders — rather than a
layout built in Python. Unlike the other `config/` files, these templates
hold no personal data (the facts are injected from `candidate.yaml` at
render time), so they're tracked in the repo and ship with a minimal
placeholder design. Swap in your own Word design by replacing these files
with the same Jinja tags; no code change is needed. The exact tags each
template expects are defined by `jobtracker/generator.py`'s
`cv_render_context`/`letter_render_context` (the data passed to the
template at render time).

The four placeholder `.docx` files are generated, not hand-edited —
`build_templates.py` is the single source for their content. Regenerate
and commit the result after any layout change there, rather than editing
the binaries directly, so they stay reproducible and diffable in spirit
(even though the committed files themselves are binary, the script that
produces them is reviewable):

```bash
python build_templates.py
```

### One-time import helper

```bash
python import_candidate.py my_cv.txt --out config/candidate.draft.yaml
```

Converts a plain-text export of your CV into a `candidate.yaml`-shaped draft
via the LLM backend configured for generation (`GENERATOR_BACKEND`). The
output is for **manual review only** — read it, correct anything wrong or
missing, then rename/copy it to `config/candidate.yaml` yourself; the script
never writes there directly. After that one-time bootstrap, `candidate.yaml`
is the single source of facts.

### Analyse + tailor pipeline (backend module)

`jobtracker/tailor.py` replaces the single "write a profile paragraph" call
with a two-step pipeline, using the official `anthropic` SDK's structured
outputs so a reply always parses into the schema in
`jobtracker/generation_schema.py`:

1. **Analyse** (Haiku, `ANTHROPIC_MODEL_EXTRACT`) extracts the vacancy's
   requirements, keywords, language, contact, deadline, and any application
   requirements (page limit, referees, a code sample) from its text.
2. **Tailor** (Sonnet, `ANTHROPIC_MODEL_WRITE`) is given that analysis plus
   `candidate.yaml`'s full bullet bank and selects/orders bullet ids and
   skill names (never writing new bullet text), writes the CV profile
   paragraph and the letter paragraphs, and produces a requirement →
   evidence map — shown only to the candidate for review, never sent to the
   employer.

A pure-code validator (`generation_schema.validate_tailor_result`) then
checks every bullet/role/skill id against `candidate.yaml`, checks word
counts, and treats a reply truncated at `max_tokens` as a failed generation;
a second, non-blocking check (`ground_tailor_result`) flags — but does not
block on — a number, year, or capitalized word in the free text that
doesn't appear anywhere in `candidate.yaml`'s facts, in case the model
invented something despite the "never invent facts" instruction. Each
analyse/tailor call is recorded (model id, token usage, stop reason, raw
response) via `jobtracker/generation_records.py`, keyed by job, for later
cost and failure auditing.

This module is unit-tested against a mocked SDK client — no live Claude API
call is made in tests. **It isn't wired into the web UI yet**: rendering a
`TailorResult` into the CV/letter templates, and a review page for editing
the result before rendering, are tracked separately.

### Generating from a bare URL

The `/ingest` page (also reachable from the "Vanaf URL" link in the web UI)
lets you generate a CV and motivation letter for a vacancy that the scraper
never picked up — for example a one-off listing outside your configured
sources. Paste the vacancy URL, or the vacancy text itself, and submit:

1. **Known platform.** If the URL matches a platform job-tracker already has
   an adapter for (Greenhouse, Lever, Ashby, SmartRecruiters, Teamtailor,
   Workday or Recruitee), job-tracker calls that platform's own
   single-posting API directly — the same parsing code the bulk scraper
   uses — so the title, location and full description come from structured
   data, not from scraping the rendered page.
2. **Generic page.** For any other URL, job-tracker fetches the page and
   extracts the main content with
   [`trafilatura`](https://trafilatura.readthedocs.io/), which strips
   navigation, footers and cookie banners.
3. **Client-rendered page.** If the extracted text is too short — typically
   because the page renders its content with JavaScript and a plain HTTP
   fetch only sees an empty shell — job-tracker falls back to Playwright when
   `PLAYWRIGHT_ENABLED=1` is set and the `playwright` package is installed.
   Without that, the page shows an error asking you to paste the vacancy text
   into the form instead, and generation proceeds from the pasted text with
   no further network access.

Every ingested vacancy is written to the `vacancy_snapshots` table (or, on
the local-JSON backend, `data/vacancy_snapshots.json`) keyed by URL, storing
the raw source and the extracted text. This keeps the vacancy text available
for later reference — for example before an interview — even after the
original posting is taken offline. Jobs generated this way are **not** added
to the main vacancy board; they only exist for the duration of the
generation request and its downloadable `.docx` files.

**Security note.** `/ingest` fetches whatever URL the form is given,
server-side, with no restriction on scheme or target host. On a trusted
single-user network this is the same risk class as any other personal tool
that fetches a URL you paste in yourself. If you run with `python run_web.py
--lan` on a network you don't fully trust, be aware that a malicious or
compromised client on that network could submit a URL pointing at an
internal host (for example the Postgres container) rather than a real
vacancy page. See issue #19 for tracked hardening work (restrict to
`http`/`https`, reject private/loopback-resolving hosts, cap response
size).

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
| werkenvoornederland | RIVM, NFI available in `config/companies.example.yaml`* | Hippo CMS component-rendering fragment (no public API) |

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

**werkenvoornederland.nl** (the central government jobs site, covering RIVM
and the Netherlands Forensic Institute) has no public API, RSS feed, or
sitemap listing individual vacancies — its `/vacatures` page is a
client-rendered shell. The `werkenvoornederland` adapter instead reaches the
site's own Hippo CMS results component directly, using the same
organisation-filter code (`werkgever=<numeric id>`) the site's own filter
checkboxes use; see the comment above `fetch_werkenvoornederland` in
`jobtracker/adapters.py` for exactly how, and how to find another
organisation's code the same way. `config/companies.example.yaml` has the
confirmed codes for RIVM and NFI.

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

The vacancy snapshots written by the `/ingest` page (see
[Generating from a bare URL](#generating-from-a-bare-url)) use the same
backend choice as `jobs`: a `vacancy_snapshots` table on Postgres, or
`data/vacancy_snapshots.json` on the local-JSON backend. Set
`PLAYWRIGHT_ENABLED=1` (with the `playwright` package installed) to let
`/ingest` render client-rendered pages before falling back to asking for
pasted text.

All three files above hold personal data and are git-ignored. If
`config/profile.yaml` or `config/candidate.yaml` is missing, the pipeline and
generator fall back to `config/profile.example.yaml` /
`config/candidate.example.yaml`, both tracked in the repo — copy whichever
you need to the real filename and fill in your own details.
`config/companies.yaml` has no such runtime fallback; `config/companies.example.yaml`
is only a reference list of entries to copy in by hand (see the Sources
section above).

`config/templates/*.docx` (the docxtpl CV/letter layouts, see
[Document layout](#document-layout-docxtpl-templates)) are the one part of
`config/` that ISN'T personal data and ISN'T git-ignored — they're tracked
like any other repo asset.

## How it fits together

```
config/companies.yaml ─┐
                        ├─► pipeline ─► adapters ─► [Job] ─► rule filter ─► LLM scorer ─► storage
config/profile.yaml ───┘       ▲                                            (Postgres | JSON | Airtable)
                               │
        run.py (CLI) ── run_web.py (FastAPI UI + APScheduler daily run)
                               │
        config/candidate.yaml ─┐
        config/templates/*.docx ─► generator (docxtpl) ─► applications/<Company>_<Title>/*.docx
```

- `jobtracker/models.py` — the `Job` schema + HTML cleaner.
- `jobtracker/adapters.py` — one function per platform; returns `list[Job]`.
- `jobtracker/filters.py` — rule filter incl. salary setpoint.
- `jobtracker/scorer.py` — LLM fit-scoring (local Ollama, or Anthropic).
- `jobtracker/generator.py` — CV + motivation letter generation: builds the
  facts/render context from `candidate.yaml` and renders it into the
  `config/templates/*.docx` layouts via docxtpl.
- `jobtracker/candidate_import.py` — one-time CV-text → `candidate.yaml`-shaped
  draft, used by `import_candidate.py`.
- `jobtracker/generation_schema.py` — shared Pydantic models (`AnalysisResult`,
  `TailorResult`) and the pure-code validator for the analyse/tailor pipeline
  below; shared by the live API path and the future JSON-handoff path (#11).
- `jobtracker/tailor.py` — the analyse (Haiku) and tailor (Sonnet) Claude API
  calls, using the official SDK's structured outputs so a reply always
  parses into the schema above.
- `jobtracker/generation_records.py` — `generation_records` storage (model
  id, token usage, stop_reason, raw response) per analyse/tailor call.
- `jobtracker/ingest.py` — generate from a bare URL/pasted text: known-platform
  detail re-fetch, trafilatura extraction, optional Playwright fallback.
- `jobtracker/snapshots.py` — `vacancy_snapshots` storage for ingested vacancies.
- `jobtracker/pipeline.py` — reusable fetch→filter→score→store run.
- `jobtracker/webapp.py` — FastAPI UI, setpoints, scheduler.
- `jobtracker/storage.py` — Postgres / local JSON / Airtable backends.
- `check_source.py` — stand-alone CLI to verify a candidate `html` source
  (and tune its `link_pattern`) before adding it to `companies.yaml`.
- `import_candidate.py` — stand-alone CLI, draft `candidate.yaml` from your
  CV text (see [One-time import helper](#one-time-import-helper)).
- `build_templates.py` — regenerates the placeholder `config/templates/*.docx`
  files (see [Document layout](#document-layout-docxtpl-templates)).

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
- [x] Generate a CV + brief from a bare vacancy URL or pasted text
- [x] docxtpl CV/letter templates + candidate.yaml bullet bank
- [x] Analyse + tailor Claude API pipeline (backend module; not yet wired
      into the web UI -- that's the review page below)
- [ ] Generation review page: requirement map, edit, regenerate, render
- [ ] E-mail/Slack digest of new high-score matches
- [ ] Playwright-based adapter for JS-rendered career sites
