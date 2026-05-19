# syntax=docker/dockerfile:1.7

############################
# Stage 1 — builder
############################
FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential libpq-dev curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /build

# Copy dependency manifests first (layer-cache friendly)
COPY pyproject.toml uv.lock README.md ./
COPY src ./src

# Sync all dependencies into the project venv using uv
RUN uv sync --frozen --no-dev

############################
# Stage 2 — runtime
############################
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    # Gradio config
    GRADIO_SERVER_NAME=0.0.0.0 \
    GRADIO_SERVER_PORT=7861

RUN apt-get update \
    && apt-get install -y --no-install-recommends libpq5 ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 chandra \
    && useradd  --uid 10001 --gid chandra --home /home/chandra --create-home --shell /bin/bash chandra

# Copy uv binary so we can use `uv run` at container start
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Copy venv + project source from builder
COPY --from=builder /build/.venv /app/.venv
COPY --from=builder /build/src   /app/src

# Copy the root-level app files
COPY --chown=chandra:chandra app.py        /app/app.py
COPY --chown=chandra:chandra graph.py      /app/graph.py
COPY --chown=chandra:chandra call_tools.py /app/call_tools.py

# Keep pyproject.toml so uv knows where the project root is
COPY --chown=chandra:chandra pyproject.toml /app/pyproject.toml
COPY --chown=chandra:chandra uv.lock        /app/uv.lock

WORKDIR /app
USER chandra

ENV PYTHONPATH=/app/src \
    PATH="/app/.venv/bin:${PATH}"

EXPOSE 7861

# `uv run` will use the already-synced .venv — no reinstall happens
ENTRYPOINT ["uv", "run", "app.py"]
