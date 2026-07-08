# Deploying job-tracker to a Raspberry Pi (self-hosted runner)

Push to `main` → the Pi's self-hosted GitHub Actions runner builds the image, runs
the offline tests, and restarts the stack with `docker compose up -d`. The web UI is
then at `http://<pi-ip>:8000`.

The stack is three containers defined in `docker-compose.yml`:

| Service | Image | Role |
|---|---|---|
| `db` | postgres:16 | storage |
| `ollama` | ollama/ollama | local LLM for scoring + letters |
| `app` | built from `Dockerfile` | web UI + daily scheduler |

(plus `ollama-pull`, a one-shot that downloads the model, then exits.)

## Memory on a 3.5 GB Pi

Peak is during scoring: db ~0.3 GB + ollama+model ~2 GB + app ~0.2 GB ≈ **~2.5 GB**,
which fits. `OLLAMA_KEEP_ALIVE=30s` unloads the model shortly after each run to give
RAM back. Model choice (in `.env`):

- `qwen2.5:1.5b` — default, good balance (~1 GB disk, ~1.5–2 GB RAM).
- `llama3.2:1b` — lightest, if RAM is tight.
- `qwen2.5:3b` — sharper, only if you have headroom.

## 1. One-time Pi prep

```bash
# Docker + compose plugin
curl -fsSL https://get.docker.com | sh
sudo apt-get install -y docker-compose-plugin
sudo usermod -aG docker $USER      # log out/in so docker works without sudo
```

## 2. Register the self-hosted runner

GitHub → repo **Settings → Actions → Runners → New self-hosted runner** → Linux/ARM64,
then run the shown commands on the Pi. Install it as a service so it survives reboots:

```bash
cd ~/actions-runner
./svc.sh install
./svc.sh start
```

The runner must be able to use Docker — the runner's user needs to be in the `docker`
group (step 1). The workflow targets `runs-on: self-hosted`; if you add labels to the
runner you can narrow it to `[self-hosted, Linux, ARM64]` in `.github/workflows/deploy.yml`.

## 3. Provision your private config (once)

`config/` and `.env` are **git-ignored** (they hold personal data), so they are NOT in
the repo. Put them in the runner's checkout directory — `clean: false` in the workflow
keeps them across deploys. The directory exists after the runner's first job, or create
it yourself:

```bash
cd ~/actions-runner/_work/job-tracker/job-tracker    # runner workspace

cp .env.example .env
nano .env                                            # set POSTGRES_PASSWORD

mkdir -p config
# copy your real config from your PC, e.g. from Windows PowerShell:
#   scp config\companies.yaml config\profile.yaml config\candidate.yaml pi@<pi-ip>:~/actions-runner/_work/job-tracker/job-tracker/config/
```

## 4. Deploy

Push to `main` (or **Actions → deploy → Run workflow**). The runner will build, test,
and start everything. First run also downloads the base images and the model, so give
it a few minutes. Then open `http://<pi-ip>:8000`.

Manual equivalents on the Pi (same directory):

```bash
docker compose up -d           # build + start
docker compose logs -f app     # watch the app
docker compose ps              # status
docker compose exec ollama ollama list   # confirm the model is present
```

## Notes

- **Scheduler**: the daily scrape runs inside the `app` container as long as it's up
  (`restart: unless-stopped` keeps it running across reboots). Set the hour on the
  Settings page of the web UI.
- **Schema**: the `jobs` table is created/migrated automatically on app start.
- **Data safety**: Postgres data (`pgdata`) and models (`ollama`) are named volumes;
  generated documents land in `./applications` on the Pi. None of these are wiped by a
  deploy.
- **Postgres access**: the DB port isn't published to the LAN by default. To browse it
  with a GUI, add `ports: ["5432:5432"]` to the `db` service.

## Troubleshooting the first deploy

The heaviest part of the first deploy is pulling the base images — especially
`ollama/ollama` (~1.5–2 GB, and it needs roughly double that free to extract). The
workflow now pulls `db` and `ollama` **one at a time with retries** in a dedicated
step, and the final `up` uses `--pull never --no-build`, so a slow download can't
hang the deploy.

**"Failed halfway through extracting" / stuck at `0B pulling fs layer`:**

1. **Disk space** is the usual culprit. On the Pi:
   ```bash
   df -h .              # is the SD card near 100%?
   docker system df     # space used by images/containers/volumes
   docker system prune -f
   ```
   If space is tight, remove old images/build cache (`docker builder prune -f`) or use
   a larger SD card. Ollama + Postgres + Python images want a few GB free.

2. **Docker Hub rate limit** (anonymous pulls are capped). Either wait and retry, or
   authenticate once on the Pi:
   ```bash
   docker login
   ```

3. **Recover a stuck run**: cancel the workflow, then on the Pi
   ```bash
   docker compose down
   docker system prune -f
   docker pull ollama/ollama:latest    # pull manually to see the real error
   ```
   Once the images are present locally, re-running the workflow is fast (the pull step
   is a no-op and `up` starts immediately).

**Reproducible pulls:** `ollama/ollama:latest` can re-download when its digest
changes. To avoid surprise re-pulls, pin a specific version in `docker-compose.yml`
(e.g. `image: ollama/ollama:0.5.7`) after confirming the tag exists for arm64.
