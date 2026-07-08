# Application image for job-tracker (FastAPI web UI + daily scheduler).
# Multi-arch base — builds natively on the Raspberry Pi (arm64) via the self-hosted runner.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies first for better layer caching. psycopg2-binary and lxml (python-docx)
# ship aarch64 wheels, so no compiler is needed on the Pi.
COPY requirements.txt ./
RUN pip install -r requirements.txt

# App code. config/, data/, applications/ are bind-mounted at runtime (see compose),
# so personal data never goes into the image.
COPY . .

EXPOSE 8000

# Serve on all interfaces so it's reachable across your LAN.
CMD ["python", "run_web.py", "--host", "0.0.0.0", "--port", "8000"]
