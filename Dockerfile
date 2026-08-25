# Base image ships headless Chromium (and every OS-level dependency it
# needs) pre-installed and version-matched — avoids manually
# apt-installing ~20 Chromium system packages by hand. The image tag's
# Playwright version MUST match requirements.txt's playwright==1.47.0
# pin exactly, or the pip-installed "playwright" Python package won't
# find a matching pre-installed browser binary on disk.
FROM mcr.microsoft.com/playwright/python:v1.47.0-jammy

WORKDIR /app

# Dependencies in their own layer so `docker build` reuses this layer on
# code-only changes (faster rebuilds/redeploys) rather than reinstalling
# every package whenever a .py file changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PYTHONUNBUFFERED=1

EXPOSE 8000

# Default command: the web dashboard. The other deployed service
# (monitoring.combined_worker) overrides this via the hosting
# platform's own per-service "Start Command" setting:
#   python -m monitoring.combined_worker --loop
#
# Shell form (not exec-array form) is required here so ${PORT} actually
# expands — most hosting platforms inject PORT at runtime for the
# public-facing service. ${PORT:-8000} keeps `docker run` usable
# standalone for local testing, where PORT is unset.
CMD ["sh", "-c", "uvicorn backend.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
