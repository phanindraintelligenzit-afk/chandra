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

WORKDIR /app

# Copy dependency manifests first (layer-cache friendly)
COPY pyproject.toml uv.lock README.md ./

# Install only third-party dependencies; skip building the local package
# (project uses a flat layout, no src/chandra package to install)
RUN uv sync --frozen --no-dev --no-install-project

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

# Copy venv from builder
COPY --from=builder /app/.venv /app/.venv

# Copy all project files
COPY . /app/

WORKDIR /app
USER chandra

ENV PYTHONPATH=/app \
    PATH="/app/.venv/bin:${PATH}" \
    GRADIO_SERVER_NAME=0.0.0.0 \
    GRADIO_SERVER_PORT=7861

EXPOSE 7861
EXPOSE 6001

CMD ["/app/start.sh"]
