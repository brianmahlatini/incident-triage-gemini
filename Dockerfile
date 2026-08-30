# Multi-stage build: the Node toolchain compiles the React console, then is
# discarded. The runtime image carries Python and the built static files only -
# shipping node_modules to production would roughly triple the image for no
# benefit and add a large dependency surface to whatever scans it.

# --------------------------------------------------------------------------- #
# Stage 1 - build the frontend
# --------------------------------------------------------------------------- #
FROM node:22-alpine AS frontend

WORKDIR /build

# Manifests first, so a source-only change reuses the cached dependency layer.
# `npm ci` (not `install`) installs exactly what the lockfile pins, which is
# what makes the build reproducible.
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci

COPY frontend/ ./
RUN npm run build

# --------------------------------------------------------------------------- #
# Stage 2 - runtime
# --------------------------------------------------------------------------- #
FROM python:3.11-slim

# PYTHONUNBUFFERED matters more than it looks: without it, stdout is block
# buffered when not a TTY, and structured log lines sit in a buffer instead of
# reaching Cloud Logging. During an incident that is the difference between
# having logs and not.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app/src \
    PORT=8080

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/
COPY eval/ ./eval/
COPY --from=frontend /build/dist ./frontend/dist

# Run as a non-root user. Cloud Run does not require it, but a container that
# only ever needs to read its own code has no reason to run as root, and it
# costs one line.
RUN useradd --create-home --uid 1001 triage && chown -R triage:triage /app
USER triage

EXPOSE 8080

# Cloud Run supplies $PORT and it is not always 8080; binding to a hardcoded
# port is the most common reason a container starts locally and fails to serve
# on Cloud Run.
#
# One worker with high concurrency is deliberate: the work is almost entirely
# waiting on the Gemini API, so a single process handles many in-flight
# requests, and Cloud Run scales out by adding instances rather than threads.
# JSON form with an explicit shell, so $PORT still expands while the
# container receives SIGTERM directly and shuts down cleanly on Cloud Run.
CMD ["sh", "-c", "exec uvicorn triage.api:app --host 0.0.0.0 --port ${PORT} --workers 1"]

# Container-level health check. Cloud Run has its own probes, but this makes
# `docker ps` honest locally and works in any plain-Docker deployment.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,os; urllib.request.urlopen(f'http://localhost:{os.environ[\"PORT\"]}/api/health').read()" || exit 1
