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
    from jobtracker import generator, webapp
    from jobtracker.storage import LocalJSONStore

    webapp._store = lambda: LocalJSONStore(str(store_path))  # patch store factory

    # generator.load_candidate() falls back to config/candidate.example.yaml
    # ONLY when config/candidate.yaml doesn't exist on disk -- true in a
    # fresh checkout, but FALSE on a host that has ever run this app for
    # real (a real candidate.yaml exists there, git-ignored, and docker
    # compose run/up both bind-mount config/ unconditionally). Several
    # tests below exercise the real (unmocked) generate/upload routes end
    # to end with hardcoded ids that only exist in the EXAMPLE candidate
    # ("examplecorp", "examplecorp-pipelines") -- against a real
    # candidate.yaml those ids don't exist, validate_tailor_result rejects
    # them, and the route silently takes its "invalid" branch instead of
    # "draft", which also means the handoff-status-restore code is never
    # reached. That's exactly the bug that broke CI/deploys on the real
    # host while passing in every clean sandbox: load_candidate() must be
    # pinned to the example file here, not left to depend on whether a
    # real candidate.yaml happens to exist on whatever machine runs this.
    _example_candidate = generator.load_candidate(
        generator.ROOT / "config" / "candidate.example.yaml")
    generator.load_candidate = lambda path=None: _example_candidate

    class _FakeRecordStore:
        """issue #13: generate/regenerate now pass a record_store through
        webapp._record_store() -- patched here (same reasoning as _store()
        above) so no test writes into the real data/generation_records.json
        on disk. .calls is asserted by the "real generate() run" test below,
        which drives a full analyse+tailor call through a fake Anthropic
        client (most other tests mock generation.* entirely, so .calls
        would stay empty for them regardless)."""
        def __init__(self):
            self.calls = []

        def record(self, **kwargs):
            self.calls.append(kwargs)
            return {"record_id": "fake-id"}

        def list_all(self):
            return []

    fake_record_store = _FakeRecordStore()
    real_record_store_fn = webapp._record_store  # saved for the "DB down" test below
    webapp._record_store = lambda: fake_record_store
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

    r = client.post("/jobs/bulk-status",
                    data={"job_ids": ["test:1", "test:2"], "status": "Reviewing"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert "bulk_updated=2" in r.headers["location"] and "bulk_total=2" in r.headers["location"]
    data = json.loads(store_path.read_text())
    assert data["test:1"]["status"] == "Reviewing" and data["test:2"]["status"] == "Reviewing"
    print("PASS bulk status update applies to every given job")

    r = client.post("/jobs/bulk-status",
                    data={"job_ids": ["test:1", "no-such-job"], "status": "Applied"},
                    follow_redirects=False)
    assert r.status_code == 303
    # one of the two ids doesn't exist -- must report 1 of 2, not silently claim success
    assert "bulk_updated=1" in r.headers["location"] and "bulk_total=2" in r.headers["location"]
    assert json.loads(store_path.read_text())["test:1"]["status"] == "Applied"
    print("PASS bulk status update reports partial success for a stale job id")

    r = client.post("/jobs/bulk-status", data={"job_ids": [], "status": "Applied"},
                    follow_redirects=False)
    assert r.status_code == 303 and "bulk_updated" not in r.headers["location"]
    print("PASS bulk status update with no job_ids is a no-op")

    before = json.loads(store_path.read_text())["test:1"]["status"]
    r = client.post("/jobs/bulk-status", data={"job_ids": ["test:1"], "status": "NotAStatus"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert json.loads(store_path.read_text())["test:1"]["status"] == before
    print("PASS bulk status update rejects a status outside STATUSES")

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

        # A stale draft (e.g. from an EARLIER successful upload/generate for
        # this job_id) must not survive a now-failed re-upload either.
        webapp.DRAFTS["test:1"] = {"result": "stale"}
        webapp.GENERATION["test:1"] = {"state": "handoff", "error": "no credit", "files": []}
        with patch("jobtracker.webapp.generation.apply_handoff_upload",
                  lambda job, data, **kw: {"state": "invalid", "errors": ["bad bullet id"]}):
            files = {"generation_file": ("generation.json", b'{"language": "en"}',
                                         "application/json")}
            r = client.post("/jobs/test:1/generation-upload", files=files, follow_redirects=False)
        assert r.status_code == 303
        assert webapp.GENERATION["test:1"]["state"] == "handoff"
        assert "bad bullet id" in webapp.GENERATION["test:1"]["error"]
        assert "test:1" not in webapp.DRAFTS
        print("PASS invalid generation.json upload stays in the handoff state and clears any stale draft")

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

        # A second regenerate while one is already in flight for this job
        # must not fire a second paid API call (double-click race).
        webapp._REGENERATING.add("test:1")
        try:
            with patch("jobtracker.webapp.generation.regenerate") as mock_regen:
                r = client.post("/jobs/test:1/review/regenerate", follow_redirects=False)
            mock_regen.assert_not_called()
        finally:
            webapp._REGENERATING.discard("test:1")
        assert r.status_code == 303
        print("PASS a regenerate already in flight for this job blocks a second one")

        with patch("jobtracker.webapp.generation.regenerate",
                  lambda *a, **kw: {"state": "handoff", "request_path": Path("/tmp/x.json"),
                                    "error": "no credit"}):
            r = client.post("/jobs/test:1/review/regenerate", follow_redirects=False)
        assert r.status_code == 303
        assert "test:1" not in webapp.DRAFTS
        assert webapp.GENERATION["test:1"]["state"] == "handoff"
        print("PASS regenerate hitting an SDK failure hands off instead of leaving a stale draft")

        draft["analysis"] = None  # reset the earlier sentinel object() back to a real value

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
        # the central claim of the review page: the CHECKED bullet (CI/CD)
        # reaches the document, and the draft's ORIGINAL bullet (pipelines
        # -- unchecked in this submission) does not.
        assert "Set up CI/CD pipelines with Docker and GitHub Actions" in cv_text
        assert "Built and maintained Python data pipelines" not in cv_text
        print("PASS review render applies edits, writes .docx, and clears the draft")

        # Real browsers normalize textarea newlines to CRLF on submit --
        # the paragraph split must not leave literal \r\n embedded.
        webapp.DRAFTS["test:1"] = draft
        webapp.GENERATION["test:1"] = {"state": "draft", "files": []}
        r = client.post("/jobs/test:1/review/render", data={
            "profile": "An edited profile.",
            "letter": "Para one.\r\n\r\nPara two.\r\n\r\nPara three.",
            "bullets": [],
        }, follow_redirects=False)
        assert r.status_code == 303 and webapp.GENERATION["test:1"]["state"] == "done"
        cv_path, letter_path = [test_out_dir / p for p in webapp.GENERATION["test:1"]["files"]]
        letter_text = "\n".join(p.text for p in docx.Document(str(letter_path)).paragraphs)
        assert "\r" not in letter_text
        assert "Para one." in letter_text and "Para two." in letter_text
        assert "Para three." in letter_text
        print("PASS review render splits CRLF-normalized textarea input into separate paragraphs")

        # A "blank" line with a stray space/tab (easy to pick up editing in
        # a textarea) must still count as the paragraph break.
        webapp.DRAFTS["test:1"] = draft
        webapp.GENERATION["test:1"] = {"state": "draft", "files": []}
        r = client.post("/jobs/test:1/review/render", data={
            "profile": "An edited profile.",
            "letter": "Para one.\n \nPara two.",
            "bullets": [],
        }, follow_redirects=False)
        assert r.status_code == 303 and webapp.GENERATION["test:1"]["state"] == "done"
        cv_path, letter_path = [test_out_dir / p for p in webapp.GENERATION["test:1"]["files"]]
        body_paragraphs = [p.text.strip() for p in docx.Document(str(letter_path)).paragraphs
                           if p.text.strip() in ("Para one.", "Para two.")]
        assert body_paragraphs == ["Para one.", "Para two."]  # two SEPARATE paragraphs, not merged
        print("PASS a whitespace-only blank line still splits paragraphs")

        # Empty profile/letter must not 422 or silently render a bodyless
        # document -- redirect back to the review page with the draft intact.
        webapp.DRAFTS["test:1"] = draft
        webapp.GENERATION["test:1"] = {"state": "draft", "files": []}
        r = client.post("/jobs/test:1/review/render",
                        data={"profile": "", "letter": "", "bullets": []},
                        follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("/jobs/test:1/review")
        assert "test:1" in webapp.DRAFTS  # draft survives, nothing was rendered
        assert webapp.GENERATION["test:1"]["state"] == "draft"
        r = client.get(r.headers["location"])
        assert r.status_code == 200 and "niet leeg zijn" in r.text
        print("PASS an empty render submission is rejected instead of 422ing or rendering nothing")

        # A stale draft from a previous (now-superseded) generation must not
        # survive a fresh "Genereer CV + brief" click -- otherwise GET
        # /jobs/{id}/review (or Render) could still serve/write it during
        # the window before the new background run finishes.
        webapp.DRAFTS["test:1"] = draft
        webapp.GENERATION["test:1"] = {"state": "draft", "files": []}
        with patch("jobtracker.webapp._generate_worker"):  # don't actually run it
            r = client.post("/jobs/test:1/generate", follow_redirects=False)
        assert r.status_code == 303
        assert "test:1" not in webapp.DRAFTS
        print("PASS a new generate() call clears any stale draft immediately")

        # --- /costs (issue #13) --------------------------------------------- #
        r = client.get("/costs")
        assert r.status_code == 200
        assert "Nog geen generaties geregistreerd" in r.text  # fake store starts empty
        print("PASS /costs renders with no generation records yet")

        fake_record_store.list_all = lambda: [
            {"job_id": "test:1", "stage": "tailor", "model": "claude-sonnet-5",
             "input_tokens": 1000, "output_tokens": 500,
             "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
             "created_at": "2026-11-20T09:00:00+00:00"},
        ]
        r = client.get("/costs")
        assert r.status_code == 200 and "2026-11" in r.text
        print("PASS /costs renders a monthly summary from recorded rows")

        fake_record_store.list_all = lambda: [
            {"job_id": "test:1", "stage": "tailor", "model": "some-future-model",
             "input_tokens": 1000, "output_tokens": 500,
             "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
             "created_at": "2026-11-20T09:00:00+00:00"},
        ]
        r = client.get("/costs")
        assert r.status_code == 200 and "—" in r.text  # unpriced model, not "$0.00"
        print("PASS /costs renders an em-dash for a model missing from MODEL_PRICING")
        fake_record_store.list_all = lambda: []

        # --- issue #13 (review fix): a dead generation_records backend must
        # not break generation or /costs, and a real end-to-end generation
        # must actually populate the record store ------------------------- #
        from jobtracker.generation_schema import AnalysisResult

        class _FakeTailorMessages:
            # generate_or_handoff doesn't thread one client object between
            # the analyse and tailor calls -- each calls make_client() on
            # its own, so a fresh _FakeTailorClient is built per call.
            # Dispatching by output_format (rather than a shared queue
            # index) is what makes a NEW client instance still answer the
            # tailor call correctly, not just the first call it happens to
            # receive.
            def __init__(self, analysis, tailor_result):
                self._analysis = analysis
                self._tailor_result = tailor_result

            def parse(self, **kwargs):
                parsed = (self._analysis if kwargs.get("output_format") is AnalysisResult
                         else self._tailor_result)
                class _Resp:
                    pass
                resp = _Resp()
                resp.parsed_output = parsed
                resp.stop_reason = "end_turn"
                resp.usage = type("U", (), {
                    "input_tokens": 100, "output_tokens": 200,
                    "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0})()
                resp.to_dict = lambda: {}
                return resp

        class _FakeTailorClient:
            def __init__(self, analysis, tailor_result):
                self.messages = _FakeTailorMessages(analysis, tailor_result)

        def _fake_client(analysis, tailor_result):
            return _FakeTailorClient(analysis, tailor_result)

        job1 = webapp._get_job("test:1")
        analysis = AnalysisResult(language="en", requirements=["Python"])
        tailor_result = _sample_result()

        with patch("jobtracker.tailor.make_client",
                  lambda: _fake_client(analysis, tailor_result)):
            fake_record_store.calls = []
            webapp.GENERATION.pop("test:1", None)  # clear leftover "running" state
            r = client.post("/jobs/test:1/generate", follow_redirects=False)
            assert r.status_code == 303
            assert webapp.GENERATION["test:1"]["state"] == "draft"
            assert {c["stage"] for c in fake_record_store.calls} == {"analyse", "tailor"}
            print("PASS a real generate() run records both API calls via _record_store()")
            webapp.DRAFTS.pop("test:1", None)

            # webapp._record_store is globally replaced with the fake above
            # (so no OTHER test writes real files) -- this specific test
            # needs the REAL _record_store() back, so its own try/except
            # (the fix under test) actually runs, with
            # generation_records.make_generation_record_store (what it
            # calls internally) raising to simulate an unreachable DB.
            with patch.object(webapp, "_record_store", real_record_store_fn), \
                patch("jobtracker.webapp.generation_records.make_generation_record_store",
                     side_effect=RuntimeError("DB down")):
                webapp.GENERATION.pop("test:1", None)
                r = client.post("/jobs/test:1/generate", follow_redirects=False)
                assert r.status_code == 303
                assert webapp.GENERATION["test:1"]["state"] == "draft"  # NOT "error"
                print("PASS a dead generation_records backend does not break generation")
                webapp.DRAFTS.pop("test:1", None)

        with patch.object(webapp, "_record_store", real_record_store_fn), \
            patch("jobtracker.webapp.generation_records.make_generation_record_store",
                 side_effect=RuntimeError("DB down")):
            r = client.get("/costs")
            assert r.status_code == 200
            assert "niet bereiken" in r.text  # degrades to a message, not a 500
            print("PASS a dead generation_records backend degrades /costs instead of 500ing")

    print("\nAll webapp tests passed.")


if __name__ == "__main__":
    main()
