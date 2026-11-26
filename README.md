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
python -m tests.test_tailor
python -m tests.test_generation_schema
python -m tests.test_generation
python -m tests.test_generation_records
python -m tests.test_ingest
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
- **CV + brief** — per vacancy, runs the analyse/tailor pipeline and opens a
  review page before anything is written: edit the profile/letter text,
  toggle which bullets render, optionally regenerate, then render to produce
  the tailored CV and motivation letter (.docx) in
  `applications/<Company>_<Title>/`. Dutch or English is auto-detected from
  the vacancy text. See [Analyse + tailor pipeline](#analyse--tailor-pipeline)
  and [Review page](#review-page) below.
- **Vanaf URL** (`/ingest`) — generate a CV + brief for a vacancy that was
  never scraped: paste its URL (or the vacancy text itself) and a tailored
  CV/letter is generated the same way as for a scraped job. See
  [Generating from a bare URL](#generating-from-a-bare-url) below.
- **Daily scrape** — APScheduler runs the pipeline once a day at the configured
  hour, as long as the web app is running.
- **Kosten** (`/costs`) — per-month spend on the analyse/tailor pipeline
  specifically, computed from the `generation_records` store; it does not
  include the local-model or Anthropic-raw-HTTP fit-scoring path (see
  [LLM fit-scoring](#llm-fit-scoring--generation--local-model-free)), which
  writes no records. See
  [Prompt caching + cost overview](#prompt-caching--cost-overview) below.

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
`cv_render_context_from_tailor_result`/`letter_render_context` (the data
the live generation path actually passes to the template at render time).
`cv_render_context` builds the same keys from the full, unfiltered
candidate.yaml instead of a `TailorResult`'s selection — no longer called
by the live path, but kept as that full-dump reference and exercised by
its own tests.

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
via the LLM backend selected by `GENERATOR_BACKEND` (default `ollama`; this
is the only thing that variable still affects — CV/letter generation itself
always goes through the Claude API pipeline below). The
output is for **manual review only** — read it, correct anything wrong or
missing, then rename/copy it to `config/candidate.yaml` yourself; the script
never writes there directly. After that one-time bootstrap, `candidate.yaml`
is the single source of facts.

### Analyse + tailor pipeline

The "Genereer CV + brief" button runs a two-step Claude API pipeline
(`jobtracker/tailor.py`), orchestrated by `jobtracker/generation.py`, instead
of writing a single free-text paragraph. It uses the official `anthropic`
SDK's structured outputs, so a *complete* reply always parses into the
schema defined in `jobtracker/generation_schema.py`. Truncation and the
SDK's own exceptions (authentication errors, rate limits, refusals) can
still occur — see `GenerationTruncatedError` and
`GenerationUnavailableError` in `tailor.py` — and are handled by the JSON
handoff described below, never by silently rendering a generic document.

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
checks every bullet, role, skill, and requirement-evidence id against
`candidate.yaml`, and treats a reply truncated at `max_tokens` as a failed
generation. These are blocking checks: a failure here means the result must
not be rendered. If the first tailor reply fails this check, `tailor.py`
retries once with the specific validation errors appended to the prompt
before giving up. A second, non-blocking check (`ground_tailor_result`)
exists for the same purpose without blocking on it — word counts outside
the prompt's target range, an explicit "N years of experience" claim, and a
number, year, or capitalized word in the free text that does not appear
anywhere in `candidate.yaml`'s facts, catching cases where the model
invents something despite the "never invent facts" instruction.
`tailor.allowed_terms(job, analysis)` feeds this check's
`extra_allowed_terms` so the vacancy's own company name and keywords — which
`TAILOR_SYSTEM` explicitly tells the model to connect the candidate's
experience to — don't themselves flag as unverified; a multi-word keyword
(e.g. "Azure DevOps") is also split into its individual words, since the
check matches one capitalized token at a time. The warnings are shown on
the review page below, never blocking rendering.

Both the two-step Claude API pipeline and the live-API call path were
exercised end to end for the first time while building the review page
below (issue #12's own acceptance criteria) — against a real vacancy, which
also caught and fixed two bugs no mocked test could: Sonnet 5's default
adaptive thinking was silently consuming the `max_tokens` budget before the
structured reply itself, truncating it well under `TAILOR_MAX_TOKENS`
(fixed by disabling thinking explicitly — neither call needs extended
reasoning), and the multi-word-keyword grounding gap above.

### Review page

Neither a successful live tailor call nor an uploaded `generation.json`
(the JSON handoff below) renders a `.docx` file directly — both produce a
**draft** and open `/jobs/{id}/review`, so nothing reaches a `.docx` file,
let alone an employer, unseen:

- A requirement → evidence table from `TailorResult.requirement_map`, and
  the `ground_tailor_result` warnings above, both read-only.
- Editable `<textarea>`s for the CV profile paragraph and the letter body.
- One checkbox per bullet in each selected role's **full** bullet bank —
  pre-checked for whatever the tailor call selected, so you can add a
  bullet it skipped or drop one it picked, not just prune its choices.
- **Regenerate**: re-runs only the tailor call against the *same* analysis
  (no re-ingest, no re-analyse) — available whenever the draft has an
  analysis to reuse, which an uploaded `generation.json` has too whenever
  its `request.json` recorded one (i.e. the analyse step had already
  succeeded before the live call failed).
- **Render**: writes the `.docx` files from whatever is currently in the
  form — the only action in the whole pipeline that writes to disk.

### JSON handoff on failure

If the pipeline cannot produce a usable result — no
`ANTHROPIC_API_KEY`/`ANTHROPIC_AUTH_TOKEN` configured, the account has no
API credit left, the reply is truncated, or it still fails validation after
the one retry — job-tracker never falls back to a generic template.
Instead it writes `applications/<Company>_<Title>/request.json` (the
vacancy, the analysis if that step already succeeded, the candidate's facts,
the exact system prompts, and the `TailorResult` JSON schema) and sets the
job's status to **"Wacht op generatie"** ("awaiting generation"), which also
appears as a selectable status on the job board. From there:

1. Download `request.json` from the job row (web UI) or the `/ingest` page
   (for a URL/paste-ingested job).
2. Run `/tailor <job_id>` in a Claude Code session opened on this repository
   (`.claude/commands/tailor.md`) — it reads `request.json`, reproduces the
   analyse/tailor steps itself, and writes `generation.json` next to it —
   or paste `request.json`'s contents into claude.ai by hand and save its
   reply as `generation.json` in the same way.
3. Upload `generation.json` from the job row. It goes through the *exact
   same* `validate_tailor_result` check a live reply would — an id that does
   not exist in `candidate.yaml` is rejected with a clear error and nothing
   is rendered — and, on success, opens the review page above, exactly like
   a successful live call.

This module is unit-tested (`tests/test_tailor.py`, `tests/test_generation.py`,
`tests/test_webapp.py`) against a mocked SDK client and against hand-built
`TailorResult`/upload payloads for day-to-day runs; see the previous
paragraph for what the one live-API run so far was for.

### Prompt caching + cost overview

The tailor call's "facts block" — `candidate.yaml`'s bullet bank, education,
skills, and style example — is identical across every vacancy for a given
candidate/language, so it's sent as a separate, cached content block
(`cache_control: {"type": "ephemeral"}`, the default 5-minute TTL) ahead of
the per-vacancy block, which is never cached. In practice this means the
second of two back-to-back tailor calls (e.g. a live generation followed by
a "Regenerate") re-uses the cached facts block instead of paying full input
price for it again — confirmed against the real API: the first call wrote
~1,900 cache tokens, the second read them back instead of rewriting them.

This only pays off when a second call actually follows within the cache's
5-minute window — the one-shot case (a single tailor call for a vacancy,
with no retry and no "Regenerate") pays a small net PREMIUM instead,
because writing the facts block into the cache costs 1.25× its normal input
price, even though nothing ever reads it back. For the ~1,900-token facts
block measured above on Sonnet 5, that premium is under a cent. The benefit
shows up specifically on the validator's one automatic retry and on
"Regenerate", both of which reuse the same facts block as a prior call to
the same vacancy within the cache window.

Every analyse/tailor call writes a row to the `generation_records` store
(`jobtracker/generation_records.py`) — model id, token usage split into
plain/cache-write/cache-read, stop_reason, and the raw response — and the
**Kosten** page (`/costs`) totals this per month, with a per-model
breakdown, using a hand-maintained pricing table in that module (there is no
pricing API to query; update `MODEL_PRICING` there if Anthropic's rates
change). A row whose model isn't in that table still counts toward
`calls`/token totals but is reported separately as `unpriced_calls` rather
than silently under-stating spend.

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

**SSRF hardening.** SSRF (server-side request forgery) is an attack where a
server is tricked into fetching a URL on the attacker's behalf, reaching
hosts the attacker could not otherwise reach directly — for example an
internal service with no public exposure. `/ingest`'s generic page fetch
(`fetch_generic` in `jobtracker/ingest.py`) is the only one of the three
paths above whose target host comes directly from user input — the
known-platform path's request host either is a hardcoded vendor API host,
or has that vendor's domain as a fixed, hardcoded SUFFIX with only the
tenant subdomain label taken from the user's URL (Teamtailor, Workday,
Recruitee); an attacker cannot make that fixed suffix resolve to an
internal address. The generic fetch itself is restricted to `http`/
`https`, resolves the host and rejects it if any resolved address is
private/loopback/link-local/multicast/reserved/unspecified, or
carrier-grade NAT (the 100.64.0.0/10 range also used by Tailscale-style
overlay networks) — covering, among others, the common internal-host and
cloud-metadata-endpoint targets. It also caps the response body read from
the socket, and re-validates the target host on every redirect hop — not
just the original URL, since a redirect can
repoint at an internal host even when the original URL was a legitimate
external one. The Playwright fallback validates its initial URL the same
way, but does not re-validate redirects the browser follows internally.
This hardening does not extend to the known-platform detail fetchers or
the `companies.yaml`-driven scrapers in `adapters.py`, since their target
host is never fully attacker-controlled. One residual gap applies even to
the generic fetch: the host is resolved once to decide whether to proceed
and resolved again, independently, by the actual HTTP request, so a
hostname whose DNS answer changes between those two lookups (DNS
rebinding) can still slip through — this hardening narrows the SSRF
attack surface, it does not close it completely. Even so, if you run with
`python run_web.py --lan` (no authentication), only do so on a network you
trust — this hardening is not a substitute for that.

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

Scoring and CV/letter generation use the Claude API independently of each
other. Scoring selects its backend through `LLM_BACKEND` (default `ollama`;
set it to `anthropic`, and set `ANTHROPIC_API_KEY`, to score with Haiku
instead of a local model). CV/letter generation has no local-model option to
select: it always runs the two-step Claude API pipeline described in
"Analyse + tailor pipeline" above — analyse (Haiku, `ANTHROPIC_MODEL_EXTRACT`,
default `claude-haiku-4-5-20251001`) and tailor (Sonnet,
`ANTHROPIC_MODEL_WRITE`, default `claude-sonnet-5`). Without
`ANTHROPIC_API_KEY`/`ANTHROPIC_AUTH_TOKEN` configured, or on any unrecovered
API failure, generation writes the JSON handoff described above instead of
falling back to a local model. `GENERATOR_BACKEND` only affects the one-time
`import_candidate.py` import helper (see "One-time import helper" above),
not CV/letter generation itself. A reply that gets cut off by the token
limit is treated as a failed generation, not returned to the caller
truncated.

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
- `jobtracker/generator.py` — the facts-loading, render-context, and
  `.docx`-writing pieces of CV + motivation letter generation: builds the
  render context from `candidate.yaml` and a `TailorResult`, and renders it
  into the `config/templates/*.docx` layouts via docxtpl.
- `jobtracker/candidate_import.py` — one-time CV-text → `candidate.yaml`-shaped
  draft, used by `import_candidate.py`.
- `jobtracker/generation_schema.py` — shared Pydantic models (`AnalysisResult`,
  `TailorResult`) and the pure-code validator for the analyse/tailor pipeline
  below; shared by the live API path and the JSON-handoff path.
- `jobtracker/tailor.py` — the analyse (Haiku) and tailor (Sonnet) Claude API
  calls, using the official SDK's structured outputs so a reply always
  parses into the schema above; also builds the JSON-handoff request content.
- `jobtracker/generation.py` — orchestrates `tailor.py` and `generator.py`
  for one job: runs the analyse/tailor pipeline into a draft for the review
  page (`regenerate()` re-runs just the tailor step; `render_draft()` is the
  only function that writes `.docx` files), or on any unrecovered failure
  writes the `request.json` handoff and flips the job's status. The only
  generation entry points `jobtracker/webapp.py` calls.
- `jobtracker/generation_records.py` — `generation_records` storage (model
  id, token usage incl. prompt-cache read/write, stop_reason, raw response)
  per analyse/tailor call, plus the pricing table and `monthly_cost_summary()`
  behind the `/costs` page.
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
- [x] Analyse + tailor Claude API pipeline, wired into the web UI
- [x] JSON handoff (request.json/generation.json) when the Claude API call
      fails, replacing the old silent template fallback
- [x] Generation review page: requirement map, edit, regenerate, render
- [x] Prompt caching for the tailor call + per-month cost overview (`/costs`)
- [ ] E-mail/Slack digest of new high-score matches
- [ ] Playwright-based adapter for JS-rendered career sites
