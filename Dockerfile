# syntax=docker/dockerfile:1.7

############################
# Stage 1 — Frontend builder (Node.js)
############################
FROM node:22-alpine AS frontend-builder

WORKDIR /app/frontend

COPY frontend/package*.json ./
RUN npm ci

COPY frontend/ .
ARG NEXT_PUBLIC_API_URL=/api/backend
ENV NEXT_PUBLIC_API_URL=$NEXT_PUBLIC_API_URL
RUN npm run build

############################
# Stage 2 — Python backend builder
############################
FROM python:3.12-slim AS backend-builder

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
# Stage 3 — runtime
############################
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    # Gradio config
    GRADIO_SERVER_NAME=0.0.0.0 \
    GRADIO_SERVER_PORT=7861

RUN apt-get update \
    && apt-get install -y --no-install-recommends libpq5 ca-certificates nginx curl \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 chandra \
    && useradd  --uid 10001 --gid chandra --home /home/chandra --create-home --shell /bin/bash chandra

# Copy uv binary so we can use `uv run` at container start
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Copy venv from backend-builder
COPY --from=backend-builder /app/.venv /app/.venv

# Copy frontend build artifacts
COPY --from=frontend-builder /app/frontend/.next /app/frontend/.next
COPY --from=frontend-builder /app/frontend/public /app/frontend/public
COPY --from=frontend-builder /app/frontend/node_modules /app/frontend/node_modules
COPY --from=frontend-builder /app/frontend/package.json /app/frontend/

# Copy all project files
COPY . /app/

RUN chmod +x /app/start.sh && \
    sed -i 's/\r$//' /app/start.sh && \
    uv pip install --python /app/.venv -e . && \
    chown -R chandra:chandra /app

WORKDIR /app
USER chandra

ENV PYTHONPATH=/app:/app/src \
    PATH="/app/.venv/bin:${PATH}" \
    GRADIO_SERVER_NAME=0.0.0.0 \
    GRADIO_SERVER_PORT=7861

CMD ["/app/start.sh"]
