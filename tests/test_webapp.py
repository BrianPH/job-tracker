"""Webapp tests with FastAPI TestClient against a temp JSON store (no network)."""
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import docx


def _seed_store(path: Path):
    jobs = {
        "test:1": {"job_id": "test:1", "title": "Bioinformatician", "company": "GenomeScan",
                   "source": "Recruitee", "url": "https://example.com/1", "location": "Leiden",
                   "remote": False, "salary": "", "description": "NGS pipelines",
                   "posted_date": "2026-07-01", "status": "New", "fit_score": 85,
                   "fit_reason": "Core bioinformatics role."},
        "test:2": {"job_id": "test:2", "title": "Remote Python Dev", "company": "RemoteCo",
                   "source": "Remotive", "url": "https://example.com/2", "location": "Worldwide",
                   "remote": True, "salary": "", "description": "APIs",
                   "posted_date": "2026-07-02", "status": "New", "fit_score": 60,
                   "fit_reason": "Backend Python work."},
    }
    path.write_text(json.dumps(jobs), encoding="utf-8")


def main():
    tmp = Path(tempfile.mkdtemp(prefix="jt_web_"))
    store_path = tmp / "jobs.json"
    _seed_store(store_path)

    # Force the JSON backend at a temp location BEFORE importing the app.
    for var in ("DATABASE_URL", "PGHOST", "PGDATABASE", "AIRTABLE_TOKEN", "AIRTABLE_BASE_ID"):
        os.environ.pop(var, None)
    os.environ["JOBTRACKER_SKIP_ENV"] = "1"

    from fastapi.testclient import TestClient
    from jobtracker import webapp
    from jobtracker.storage import LocalJSONStore

    webapp._store = lambda: LocalJSONStore(str(store_path))  # patch store factory
    client = TestClient(webapp.app)

    r = client.get("/")
    assert r.status_code == 200
    assert "Bioinformatician" in r.text and "Remote Python Dev" in r.text
    assert "https://example.com/1" in r.text  # link to original vacancy
    print("PASS index lists jobs with original links")

    r = client.get("/?min_score=70")
    assert "Bioinformatician" in r.text and "Remote Python Dev" not in r.text
    print("PASS min_score filter")

    r = client.get("/?status=&company=&source=&q=&min_score=&remote=0")
    assert r.status_code == 200
    assert "Bioinformatician" in r.text and "Remote Python Dev" in r.text
    print("PASS empty min_score does not error")

    r = client.get("/?remote=1")
    assert "Remote Python Dev" in r.text and "Bioinformatician" not in r.text
    print("PASS remote filter")

    r = client.post("/jobs/test:1/status", data={"status": "Applied"}, follow_redirects=False)
    assert r.status_code == 303
    assert json.loads(store_path.read_text())["test:1"]["status"] == "Applied"
    print("PASS status update persists")

    r = client.post("/jobs/test:2/status", data={
        "status": "Rejected", "f_min_score": "50", "f_remote": "1",
    }, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/?min_score=50&remote=1"
    assert json.loads(store_path.read_text())["test:2"]["status"] == "Rejected"
    print("PASS status update preserves active filters")

    r = client.get("/settings")
    assert r.status_code == 200 and "Minimum salaris" in r.text
    print("PASS settings page renders setpoints")

    r = client.get("/scrape/log")
    assert r.status_code == 200
    print("PASS scrape log endpoint")

    assert "Herscoor alles" in client.get("/").text
    print("PASS rescore button present")

    # --- /ingest: generate a CV + brief from a bare URL or pasted text --- #
    r = client.get("/ingest")
    assert r.status_code == 200 and "Vacature-URL" in r.text
    print("PASS ingest form renders")

    r = client.post("/ingest", data={"url": "", "paste_text": ""}, follow_redirects=True)
    assert r.status_code == 200
    assert "A vacancy URL or pasted text is required." in r.text
    print("PASS ingest with neither url nor paste shows the input-error message")

    # A schemeless URL ("acme.com/careers/1", no "https://") raises
    # requests.exceptions.MissingSchema, which also subclasses ValueError --
    # this must land in the generic/sanitized branch, not be reflected as the
    # raw library message (the bug finding C described).
    r = client.post("/ingest", data={"url": "acme.com/careers/1", "paste_text": ""},
                    follow_redirects=True)
    assert r.status_code == 200
    assert "MissingSchema" not in r.text and "Invalid URL" not in r.text
    assert "mislukt" in r.text.lower()
    print("PASS ingest schemeless url does not leak the raw requests exception text")

    fake_job = {"job_id": "ingest:abc123", "title": "Data Engineer", "company": "Acme",
               "source": "Ingested", "url": "https://acme.example/careers/1",
               "description": "Build things.", "location": "", "remote": False,
               "salary": "", "posted_date": "", "status": "New", "fit_score": None,
               "fit_reason": "", "fit_persona": ""}
    with patch("jobtracker.webapp.ingest_url", lambda url, paste_text=None: fake_job), \
         patch("jobtracker.webapp._generate_worker",
              lambda job_id, job: webapp.GENERATION.update(
                  {job_id: {"state": "done", "files": [], "llm_used": False, "lang": "en"}})):
        r = client.post("/ingest", data={"url": "https://acme.example/careers/1",
                                         "paste_text": ""}, follow_redirects=True)
    assert r.status_code == 200
    assert "Data Engineer" in r.text and "Acme" in r.text
    assert webapp.ADHOC_JOBS.get("ingest:abc123") == fake_job
    print("PASS ingest success path shows the generated job and stores it in ADHOC_JOBS")

    # --- issue #11/M3b: JSON handoff routes ------------------------------- #
    from jobtracker.generation import HANDOFF_STATUS
    assert HANDOFF_STATUS in webapp.STATUSES
    print("PASS handoff status is a selectable job status")

    request_json_path = tmp / "request.json"
    request_json_path.write_text('{"job": {"title": "x"}}', encoding="utf-8")
    webapp.GENERATION["test:1"] = {"state": "handoff", "error": "no credit",
                                   "request_path": str(request_json_path), "files": []}

    r = client.get("/jobs/test:1/request.json")
    assert r.status_code == 200 and '"title": "x"' in r.text
    print("PASS request.json download serves the handoff file")

    r = client.get("/")
    assert r.status_code == 200
    assert "download request.json" in r.text and "upload generation.json" in r.text
    print("PASS index page renders the handoff download/upload block without error")

    webapp.ADHOC_JOBS["adhoc:handoff"] = {**fake_job, "job_id": "adhoc:handoff"}
    webapp.GENERATION["adhoc:handoff"] = {"state": "handoff", "error": "no credit",
                                          "request_path": str(request_json_path), "files": []}
    r = client.get("/ingest?job_id=adhoc:handoff")
    assert r.status_code == 200
    assert "Upload generation.json" in r.text
    webapp.ADHOC_JOBS.pop("adhoc:handoff", None)
    webapp.GENERATION.pop("adhoc:handoff", None)
    print("PASS ingest page renders the handoff download/upload block without error")

    r = client.get("/jobs/unknown-job/request.json")
    assert r.status_code == 404
    print("PASS request.json download 404s for a job with no handoff state")

    webapp.GENERATION.pop("test:1", None)
    r = client.get("/jobs/test:1/request.json")
    assert r.status_code == 404
    print("PASS request.json download 404s once there is no handoff state at all")

    # The routes/tests below render or write request.json for real (not
    # mocked) -- _generate_worker and the upload route both pass
    # out_dir=OUTPUT_DIR explicitly (webapp's own module global, read at
    # call time), so patching webapp.OUTPUT_DIR redirects every one of
    # them at a throwaway tmp dir instead of the repo's real applications/
    # (which docker-compose bind-mounts into the running container --
    # writing or deleting folders there in a test would touch the user's
    # actual generated documents).
    test_out_dir = tmp / "applications"
    with patch.object(webapp, "OUTPUT_DIR", test_out_dir):
        # GENERATION is purely in-memory and would be empty right after a
        # restart -- the job's persisted status + request.json on disk must
        # be enough on their own to keep the download/upload UI reachable.
        handoff_folder = test_out_dir / "GenomeScan_Bioinformatician"
        handoff_folder.mkdir(parents=True)
        (handoff_folder / "request.json").write_text(
            json.dumps({"error": "no credit left", "previous_status": "New"}), encoding="utf-8")
        webapp._store().update_status("test:1", HANDOFF_STATUS)
        try:
            r = client.get("/")
            assert r.status_code == 200 and "download request.json" in r.text
            r = client.get("/jobs/test:1/request.json")
            assert r.status_code == 200 and "no credit left" in r.text
            print("PASS handoff UI and download survive a restart via status + request.json on disk")
        finally:
            webapp._store().update_status("test:1", "New")

        fake_draft = {"state": "draft", "job": {"job_id": "test:1"}, "candidate": {},
                     "analysis": None, "lang": "en", "style_example": "",
                     "result": object()}

        with patch("jobtracker.webapp.generation.apply_handoff_upload",
                  lambda job, data, **kw: fake_draft):
            files = {"generation_file": ("generation.json", b'{"language": "en"}',
                                         "application/json")}
            r = client.post("/jobs/test:1/generation-upload", files=files, follow_redirects=False)
        assert r.status_code == 303
        assert webapp.GENERATION["test:1"]["state"] == "draft"
        assert webapp.DRAFTS["test:1"] is fake_draft
        print("PASS valid generation.json upload produces a draft and clears the handoff state")

        webapp.GENERATION["test:1"] = {"state": "handoff", "error": "no credit", "files": []}
        with patch("jobtracker.webapp.generation.apply_handoff_upload",
                  lambda job, data, **kw: {"state": "invalid", "errors": ["bad bullet id"]}):
            files = {"generation_file": ("generation.json", b'{"language": "en"}',
                                         "application/json")}
            r = client.post("/jobs/test:1/generation-upload", files=files, follow_redirects=False)
        assert r.status_code == 303
        assert webapp.GENERATION["test:1"]["state"] == "handoff"
        assert "bad bullet id" in webapp.GENERATION["test:1"]["error"]
        print("PASS invalid generation.json upload stays in the handoff state with the errors shown")

        files = {"generation_file": ("generation.json", b"not json at all", "application/json")}
        r = client.post("/jobs/test:1/generation-upload", files=files, follow_redirects=False)
        assert r.status_code == 303
        assert "not valid JSON" in webapp.GENERATION["test:1"]["error"]
        print("PASS a non-JSON upload is rejected without calling the validator")

        r = client.post("/jobs/unknown-job/generation-upload",
                        files={"generation_file": ("g.json", b"{}", "application/json")},
                        follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/"
        print("PASS generation-upload for an unknown job redirects without error")

        # --- _set_status_fn: DB-backed job vs. ADHOC_JOBS job go to
        # different sinks -- exercised through the real (unmocked) upload
        # route, so this proves the actual wiring, not just the helper in
        # isolation.
        valid_generation_json = {
            "language": "en", "profile": "A tailored profile. " * 10,
            "experience_order": ["examplecorp"],
            "bullets": {"examplecorp": ["examplecorp-pipelines"]},
            "skills": ["Python"],
            "letter_paragraphs": ["Paragraph one. " * 20, "Paragraph two. " * 20],
        }

        webapp._store().update_status("test:2", HANDOFF_STATUS)
        try:
            files = {"generation_file": ("generation.json",
                                         json.dumps(valid_generation_json).encode(),
                                         "application/json")}
            r = client.post("/jobs/test:2/generation-upload", files=files, follow_redirects=False)
            assert r.status_code == 303
            assert json.loads(store_path.read_text())["test:2"]["status"] == "New"
            assert webapp.GENERATION["test:2"]["state"] == "draft"
            assert webapp.DRAFTS["test:2"]["result"].profile == valid_generation_json["profile"]
            print("PASS generation-upload for a DB-backed job restores status via store.update_status")
        finally:
            webapp._store().update_status("test:2", "New")

        webapp.ADHOC_JOBS["ingest:abc123"]["status"] = HANDOFF_STATUS
        files = {"generation_file": ("generation.json",
                                     json.dumps(valid_generation_json).encode(),
                                     "application/json")}
        r = client.post("/jobs/ingest:abc123/generation-upload", files=files, follow_redirects=False)
        assert r.status_code == 303
        assert webapp.ADHOC_JOBS["ingest:abc123"]["status"] == "New"
        # must NOT have touched the real job store for an id it has no row for
        assert "ingest:abc123" not in json.loads(store_path.read_text())
        print("PASS generation-upload for an ADHOC job restores status in ADHOC_JOBS, not the store")
        webapp.DRAFTS.clear()

        # --- review page (issue #12): GET/regenerate/render ---------------- #
        from jobtracker.generation_schema import TailorResult

        def _sample_result(**overrides):
            base = dict(
                language="en", profile="A tailored profile. " * 10,
                experience_order=["examplecorp"], bullets={"examplecorp": ["examplecorp-pipelines"]},
                skills=["Python"], letter_paragraphs=["Paragraph one. " * 20, "Paragraph two. " * 20],
            )
            base.update(overrides)
            return TailorResult(**base)

        job1 = webapp._get_job("test:1")
        draft = {"job": job1, "candidate": webapp.generator.load_candidate(), "analysis": None,
                "lang": "en", "style_example": "", "result": _sample_result()}
        webapp.DRAFTS["test:1"] = draft
        webapp.GENERATION["test:1"] = {"state": "draft", "files": []}

        r = client.get("/jobs/test:1/review")
        assert r.status_code == 200
        assert "A tailored profile." in r.text
        assert 'value="examplecorp::examplecorp-pipelines"' in r.text
        assert "checked" in r.text
        print("PASS review page renders the draft's profile, letter, and bullet checkboxes")

        r = client.get("/")
        assert r.status_code == 200 and f'/jobs/test:1/review">' in r.text
        print("PASS job board links to the review page while a draft is pending")

        r = client.get("/jobs/unknown-job/review", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/"
        print("PASS review page redirects for a job with no draft")

        # Regenerate: no analysis on this draft -> must be a no-op, not call
        # generation.regenerate at all (it needs the SAME analysis; there
        # isn't one here).
        with patch("jobtracker.webapp.generation.regenerate") as mock_regen:
            r = client.post("/jobs/test:1/review/regenerate", follow_redirects=False)
        assert r.status_code == 303
        mock_regen.assert_not_called()
        print("PASS regenerate without an analysis is a no-op")

        draft["analysis"] = object()  # just needs to be not-None for this check
        regenerated = _sample_result(profile="A regenerated profile. " * 10)
        with patch("jobtracker.webapp.generation.regenerate",
                  lambda *a, **kw: {"state": "draft", "result": regenerated, "errors": []}):
            r = client.post("/jobs/test:1/review/regenerate", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/jobs/test:1/review"
        assert webapp.DRAFTS["test:1"]["result"] is regenerated
        print("PASS regenerate updates the stored draft")

        with patch("jobtracker.webapp.generation.regenerate",
                  lambda *a, **kw: {"state": "handoff", "request_path": Path("/tmp/x.json"),
                                    "error": "no credit"}):
            r = client.post("/jobs/test:1/review/regenerate", follow_redirects=False)
        assert r.status_code == 303
        assert "test:1" not in webapp.DRAFTS
        assert webapp.GENERATION["test:1"]["state"] == "handoff"
        print("PASS regenerate hitting an SDK failure hands off instead of leaving a stale draft")

        # Render: edits + bullet selection actually reach generation.render_draft,
        # and the draft is cleared afterward.
        webapp.DRAFTS["test:1"] = draft
        webapp.GENERATION["test:1"] = {"state": "draft", "files": []}
        r = client.post("/jobs/test:1/review/render", data={
            "profile": "An edited profile.",
            "letter": "Edited paragraph one.\n\nEdited paragraph two.",
            "bullets": ["examplecorp::examplecorp-cicd"],
        }, follow_redirects=False)
        assert r.status_code == 303
        assert webapp.GENERATION["test:1"]["state"] == "done"
        assert "test:1" not in webapp.DRAFTS
        cv_path, letter_path = [test_out_dir / p for p in webapp.GENERATION["test:1"]["files"]]
        letter_text = "\n".join(p.text for p in docx.Document(str(letter_path)).paragraphs)
        assert "Edited paragraph one." in letter_text
        cv_text = "\n".join(p.text for p in docx.Document(str(cv_path)).paragraphs)
        assert "An edited profile." in cv_text
        print("PASS review render applies edits, writes .docx, and clears the draft")

    print("\nAll webapp tests passed.")


if __name__ == "__main__":
    main()
