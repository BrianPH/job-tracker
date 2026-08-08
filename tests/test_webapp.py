"""Webapp tests with FastAPI TestClient against a temp JSON store (no network)."""
import json
import os
import tempfile
from pathlib import Path


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

    r = client.get("/settings")
    assert r.status_code == 200 and "Minimum salaris" in r.text
    print("PASS settings page renders setpoints")

    r = client.get("/scrape/log")
    assert r.status_code == 200
    print("PASS scrape log endpoint")

    assert "Herscoor alles" in client.get("/").text
    print("PASS rescore button present")

    print("\nAll webapp tests passed.")


if __name__ == "__main__":
    main()
