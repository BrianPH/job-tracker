"""Phase 5: local web UI + daily scheduler.

Start with:  python run_web.py   (serves http://127.0.0.1:8000)

Features
- Job board: filter by status/company/source/min score/remote/search, sorted by
  fit score; every row links to the ORIGINAL vacancy.
- Setpoints page (/settings): edit keywords, excludes, locations, minimum salary,
  remote rule and the daily scrape hour — written back to config/profile.yaml.
- "Scrape nu" button: runs the pipeline in a background thread, live log shown.
- Per-job "Genereer CV + brief": runs the analyse/tailor Claude API pipeline
  (jobtracker/generation.py) in a background thread; download links appear on
  the job row. On any unrecovered failure the job gets a JSON handoff instead
  (issue #11/M3b): download request.json, run `.claude/commands/tailor.md` or
  paste it into claude.ai, upload the resulting generation.json.
- APScheduler: runs the pipeline automatically once a day (hour configurable).
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlencode

import yaml
from fastapi import BackgroundTasks, FastAPI, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from . import generation
from . import generator
from .generator import OUTPUT_DIR
from .ingest import IngestInputError, ThinContentError, ingest_url
from .pipeline import ROOT, load_env, load_profile, run_pipeline
from .storage import make_store

app = FastAPI(title="job-tracker")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

STATUSES = ["New", "Reviewing", "Applied", "Rejected", generation.HANDOFF_STATUS]

# --- in-memory state (single local user) ------------------------------------ #
SCRAPE = {"running": False, "log": [], "last_summary": None}
# job_id -> {"state": "running"|"done"|"handoff"|"error", "files": [...], "error": str,
#            "request_path": str (handoff only)}
GENERATION = {}
ADHOC_JOBS = {}  # job_id -> job dict, for jobs ingested from a URL/paste (not in the DB)
_LOCK = threading.Lock()


def _store():
    load_env()
    return make_store()[0]


# --- scraping ---------------------------------------------------------------- #
def _scrape_worker(rescore: bool = False):
    SCRAPE["log"] = []
    try:
        summary = run_pipeline(rescore=rescore, log=lambda line: SCRAPE["log"].append(line))
        SCRAPE["last_summary"] = summary
    except Exception as e:  # noqa: BLE001
        SCRAPE["log"].append(f"ERROR: {e}")
    finally:
        SCRAPE["running"] = False


def start_scrape(rescore: bool = False) -> bool:
    with _LOCK:
        if SCRAPE["running"]:
            return False
        SCRAPE["running"] = True
    threading.Thread(target=_scrape_worker, kwargs={"rescore": rescore}, daemon=True).start()
    return True


# --- handoff recovery (survives a restart; GENERATION does not) -------------- #
def _handoff_view(job: Optional[dict]) -> Optional[dict]:
    """Reconstructs a GENERATION-shaped "handoff" entry from the job's
    persisted status and request.json on disk. GENERATION is purely
    in-memory and empty again after a restart, but the job's status
    ("Wacht op generatie") and request.json both survive one -- without
    this, the download/upload UI would silently disappear on restart even
    though the job is still, correctly, waiting on a human (see deploy.yml,
    which restarts the container on every push)."""
    if job is None or job.get("status") != generation.HANDOFF_STATUS:
        return None
    candidate = generator.load_candidate()
    lang = generator.detect_language(f"{job.get('title','')} {job.get('description','')}")
    path = generator.application_paths(job, candidate, lang, OUTPUT_DIR)[0].parent / "request.json"
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        payload = {}
    return {"state": "handoff", "error": payload.get("error", ""),
           "request_path": str(path), "files": []}


def _generation_view(jobs: list) -> dict:
    """GENERATION, with a synthesized handoff entry added for any job whose
    status says it's waiting on a human but has no (fresher) in-memory
    entry -- e.g. right after a restart."""
    view = dict(GENERATION)
    for job in jobs:
        job_id = job.get("job_id")
        if job_id and job_id not in view:
            synthesized = _handoff_view(job)
            if synthesized is not None:
                view[job_id] = synthesized
    return view


# --- routes ------------------------------------------------------------------ #
@app.get("/", response_class=HTMLResponse)
def index(request: Request, status: str = "", company: str = "", source: str = "",
          q: str = "", min_score: str = "", remote: int = 0):
    store = _store()
    try:
        min_score_val = int(min_score) if min_score.strip() else None
    except ValueError:
        min_score_val = None
    jobs = store.fetch(status=status or None, company=company or None,
                       min_score=min_score_val)
    if source:
        jobs = [j for j in jobs if (j.get("source") or "").lower() == source.lower()]
    if q:
        needle = q.lower()
        jobs = [j for j in jobs if needle in (j.get("title") or "").lower()
                or needle in (j.get("company") or "").lower()]
    if remote:
        jobs = [j for j in jobs if j.get("remote")]

    sources = sorted({j.get("source") or "" for j in store.fetch()} - {""})
    return templates.TemplateResponse(request, "index.html", {
        "jobs": jobs, "statuses": STATUSES, "sources": sources,
        "flt": {"status": status, "company": company, "source": source,
                "q": q, "min_score": min_score, "remote": remote},
        "scrape": SCRAPE, "generation": _generation_view(jobs),
    })


@app.post("/jobs/{job_id}/status")
def set_status(job_id: str, status: str = Form(...),
               f_status: str = Form(""), f_company: str = Form(""), f_source: str = Form(""),
               f_q: str = Form(""), f_min_score: str = Form(""), f_remote: str = Form("")):
    if status in STATUSES:
        _store().update_status(job_id, status)
    params = {k: v for k, v in {
        "status": f_status, "company": f_company, "source": f_source,
        "q": f_q, "min_score": f_min_score, "remote": f_remote,
    }.items() if v}
    target = f"/?{urlencode(params)}" if params else "/"
    return RedirectResponse(target, status_code=303)


@app.post("/scrape")
def scrape():
    start_scrape()
    return RedirectResponse("/", status_code=303)


@app.post("/rescore")
def rescore():
    """Full re-score: run the pipeline scoring ALL kept jobs again. Use after
    changing the model (OLLAMA_MODEL) or the personas in profile.yaml —
    existing scores are otherwise preserved on re-scrapes by design."""
    start_scrape(rescore=True)
    return RedirectResponse("/", status_code=303)


@app.get("/scrape/log", response_class=HTMLResponse)
def scrape_log():
    state = "bezig..." if SCRAPE["running"] else "klaar"
    lines = "\n".join(SCRAPE["log"][-60:])
    return HTMLResponse(f"<pre>status: {state}\n{lines}</pre>")


# --- CV / letter generation --------------------------------------------------- #
def _set_status_fn(job_id: str) -> Callable[[str], None]:
    """ADHOC_JOBS (ingested from a URL/paste) aren't rows in the real store --
    their status lives only in that in-memory dict, so a handoff/upload on
    one of them must update it there instead of calling store.update_status
    (which would silently no-op: no row with that job_id exists)."""
    if job_id in ADHOC_JOBS:
        def _set(status: str) -> None:
            ADHOC_JOBS[job_id]["status"] = status
        return _set

    def _set(status: str) -> None:
        _store().update_status(job_id, status)
    return _set


def _redirect_target(job_id: str) -> str:
    return f"/ingest?job_id={job_id}" if job_id in ADHOC_JOBS else "/"


def _generate_worker(job_id: str, job: dict):
    try:
        result = generation.generate_or_handoff(job, set_status=_set_status_fn(job_id))
    except Exception as e:  # noqa: BLE001
        GENERATION[job_id] = {"state": "error", "error": str(e), "files": []}
        return
    if result["state"] == "handoff":
        GENERATION[job_id] = {"state": "handoff", "error": result["error"],
                              "request_path": str(result["request_path"]), "files": []}
    else:
        rel = [str(p.relative_to(OUTPUT_DIR)).replace("\\", "/") for p in
               (result["cv"], result["letter"])]
        GENERATION[job_id] = {"state": "done", "files": rel, "lang": result["lang"]}


def _get_job(job_id: str) -> Optional[dict]:
    return _store().get(job_id) or ADHOC_JOBS.get(job_id)


@app.post("/jobs/{job_id}/generate")
def generate(job_id: str, background_tasks: BackgroundTasks):
    job = _get_job(job_id)
    if job is None:
        return RedirectResponse("/", status_code=303)
    current = GENERATION.get(job_id, {})
    if current.get("state") != "running":
        GENERATION[job_id] = {"state": "running", "files": []}
        background_tasks.add_task(_generate_worker, job_id, job)
    return RedirectResponse(_redirect_target(job_id), status_code=303)


@app.get("/jobs/{job_id}/request.json")
def download_request_json(job_id: str):
    gen = GENERATION.get(job_id) or {}
    path_str = gen.get("request_path") if gen.get("state") == "handoff" else None
    if not path_str:
        synthesized = _handoff_view(_get_job(job_id))
        path_str = synthesized.get("request_path") if synthesized else None
    if not path_str:
        return HTMLResponse("Not found", status_code=404)
    path = Path(path_str)
    if not path.is_file():
        return HTMLResponse("Not found", status_code=404)
    return FileResponse(str(path), filename="request.json")


@app.post("/jobs/{job_id}/generation-upload")
async def generation_upload(job_id: str, generation_file: UploadFile = File(...)):
    job = _get_job(job_id)
    if job is None:
        return RedirectResponse("/", status_code=303)

    raw = await generation_file.read()
    try:
        data = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        current = GENERATION.get(job_id, {})
        GENERATION[job_id] = {**current, "state": "handoff",
                              "error": "Uploaded file is not valid JSON."}
        return RedirectResponse(_redirect_target(job_id), status_code=303)

    result = generation.apply_handoff_upload(job, data, set_status=_set_status_fn(job_id))
    if result["state"] == "invalid":
        current = GENERATION.get(job_id, {})
        GENERATION[job_id] = {**current, "state": "handoff",
                              "error": "; ".join(result["errors"])}
    else:
        rel = [str(p.relative_to(OUTPUT_DIR)).replace("\\", "/") for p in
               (result["cv"], result["letter"])]
        GENERATION[job_id] = {"state": "done", "files": rel, "lang": result["lang"]}
    return RedirectResponse(_redirect_target(job_id), status_code=303)


# --- generate from a bare URL / pasted text ----------------------------------- #
@app.get("/ingest", response_class=HTMLResponse)
def ingest_form(request: Request, job_id: str = ""):
    job = ADHOC_JOBS.get(job_id) if job_id else None
    return templates.TemplateResponse(request, "ingest.html", {
        "job": job, "job_id": job_id, "generation": _generation_view([job] if job else []),
        "error": "", "url": "",
    })


@app.post("/ingest")
def ingest_submit(background_tasks: BackgroundTasks,
                  url: str = Form(""), paste_text: str = Form("")):
    try:
        job = ingest_url(url, paste_text or None)
    except ThinContentError as e:
        print(f"  [ingest] thin content for {e.url} ({len(e.raw_html)} raw chars fetched)")
        params = urlencode({"error": "thin", "url": url})
        return RedirectResponse(f"/ingest/error?{params}", status_code=303)
    except IngestInputError as e:
        # user input error (neither url nor paste_text given) -- safe to show as-is.
        # Deliberately NOT a bare `except ValueError`: several requests.exceptions
        # classes (MissingSchema, InvalidSchema, InvalidURL -- e.g. a URL pasted
        # without "https://") also subclass ValueError, and their messages are
        # library-internal wording that must go through the generic branch below.
        params = urlencode({"error": str(e), "url": url})
        return RedirectResponse(f"/ingest/error?{params}", status_code=303)
    except Exception as e:  # noqa: BLE001 - don't reflect raw exception text into the page
        print(f"  [ingest] fetch failed for {url}: {e}")
        params = urlencode({"error": "fetch_failed", "url": url})
        return RedirectResponse(f"/ingest/error?{params}", status_code=303)

    job_id = job["job_id"]
    ADHOC_JOBS[job_id] = job
    current = GENERATION.get(job_id, {})
    if current.get("state") != "running":
        GENERATION[job_id] = {"state": "running", "files": []}
        background_tasks.add_task(_generate_worker, job_id, job)
    return RedirectResponse(f"/ingest?job_id={job_id}", status_code=303)


@app.get("/ingest/error", response_class=HTMLResponse)
def ingest_error(request: Request, error: str = "", url: str = ""):
    if error == "thin":
        message = ("Kon de vacaturetekst niet automatisch ophalen (waarschijnlijk een "
                  "client-rendered pagina). Plak de vacaturetekst hieronder en probeer opnieuw.")
    elif error == "fetch_failed":
        message = ("Ophalen van de URL is mislukt. Controleer de URL, of plak de "
                  "vacaturetekst hieronder.")
    else:
        message = error  # a safe, user-input-level ValueError message
    return templates.TemplateResponse(request, "ingest.html", {
        "job": None, "job_id": "", "generation": GENERATION, "error": message, "url": url,
    })


@app.get("/applications/{path:path}")
def download(path: str):
    target = (OUTPUT_DIR / path).resolve()
    if not str(target).startswith(str(OUTPUT_DIR.resolve())) or not target.is_file():
        return HTMLResponse("Not found", status_code=404)
    return FileResponse(str(target), filename=target.name)


# --- settings (setpoints) ------------------------------------------------------ #
def _profile_path() -> Path:
    return ROOT / "config" / "profile.yaml"


@app.get("/settings", response_class=HTMLResponse)
def settings(request: Request):
    profile = load_profile()
    rules = profile.get("rules", {}) or {}
    appcfg = profile.get("app", {}) or {}
    return templates.TemplateResponse(request, "settings.html", {
        "keywords": "\n".join(rules.get("keywords_any", [])),
        "excludes": "\n".join(rules.get("exclude_any", [])),
        "locations": "\n".join(rules.get("locations_any", [])),
        "always_keep_remote": rules.get("always_keep_remote", True),
        "min_salary": rules.get("min_salary", 0) or 0,
        "scrape_hour": appcfg.get("scrape_hour", 8),
        "summary": (profile.get("profile", {}) or {}).get("summary", ""),
        "saved": request.query_params.get("saved") == "1",
    })


def _lines(text: str) -> list:
    return [l.strip() for l in (text or "").splitlines() if l.strip()]


@app.post("/settings")
def save_settings(keywords: str = Form(""), excludes: str = Form(""),
                  locations: str = Form(""), always_keep_remote: Optional[str] = Form(None),
                  min_salary: int = Form(0), scrape_hour: int = Form(8),
                  summary: str = Form("")):
    profile = load_profile()
    profile.setdefault("rules", {})
    profile["rules"]["keywords_any"] = _lines(keywords)
    profile["rules"]["exclude_any"] = _lines(excludes)
    profile["rules"]["locations_any"] = _lines(locations)
    profile["rules"]["always_keep_remote"] = always_keep_remote is not None
    profile["rules"]["min_salary"] = max(0, int(min_salary))
    profile.setdefault("app", {})["scrape_hour"] = min(23, max(0, int(scrape_hour)))
    if summary.strip():
        profile.setdefault("profile", {})["summary"] = summary.strip()
    _profile_path().write_text(
        yaml.safe_dump(profile, allow_unicode=True, sort_keys=False, width=100),
        encoding="utf-8")
    _reschedule()
    return RedirectResponse("/settings?saved=1", status_code=303)


# --- daily scheduler ------------------------------------------------------------ #
_scheduler = None


def _scrape_job():
    start_scrape()


def _reschedule():
    global _scheduler
    if _scheduler is None:
        return
    hour = (load_profile().get("app", {}) or {}).get("scrape_hour", 8)
    _scheduler.remove_all_jobs()
    _scheduler.add_job(_scrape_job, "cron", hour=int(hour), minute=0,
                       id="daily_scrape", replace_existing=True)


@app.on_event("startup")
def _start_scheduler():
    global _scheduler
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
    except ImportError:
        print("APScheduler niet geïnstalleerd — dagelijkse run uitgeschakeld "
              "(pip install apscheduler).")
        return
    _scheduler = BackgroundScheduler()
    _scheduler.start()
    _reschedule()
    hour = (load_profile().get("app", {}) or {}).get("scrape_hour", 8)
    print(f"Dagelijkse scrape gepland om {int(hour):02d}:00 (zolang de webapp draait).")
