# Institutional-Grade Dockerfile for Agent Swarm
FROM python:3.11-slim as base

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install uv for fast dependency management (pinned: non-mutable tag for determinism)
COPY --from=ghcr.io/astral-sh/uv:0.4.25 /uv /bin/uv

WORKDIR /app

# Copy dependency files first
COPY pyproject.toml .
# If we had a lock file: COPY uv.lock .

# L-02: INSTALL_EXTRAS controls optional dependencies built into the image.
# Base image (FILE state store only):    docker build .
# Redis-enabled image:                   docker build --build-arg INSTALL_EXTRAS=redis .
ARG INSTALL_EXTRAS=""

# Install dependencies using uv
RUN uv pip install --system ".[${INSTALL_EXTRAS}]"

# Copy source code
COPY src/ src/
COPY apps/ apps/
COPY agents/ agents/
COPY workflows/ workflows/

# Expose API port
EXPOSE 8000

ENV PYTHONPATH="/app/src:${PYTHONPATH}"

CMD ["uvicorn", "apps.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
