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

RUN pip install --no-cache-dir uv

WORKDIR /build
COPY pyproject.toml README.md ./
COPY src ./src

RUN uv venv /opt/venv \
    && . /opt/venv/bin/activate \
    && uv pip install --no-cache .

############################
# Stage 2 — runtime
############################
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:${PATH}"

RUN apt-get update \
    && apt-get install -y --no-install-recommends libpq5 ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 chandra \
    && useradd  --uid 10001 --gid chandra --home /home/chandra --create-home --shell /bin/bash chandra

COPY --from=builder /opt/venv /opt/venv
COPY --chown=chandra:chandra src /app/src
COPY --chown=chandra:chandra iac /app/iac
COPY --chown=chandra:chandra evals /app/evals

WORKDIR /app
USER chandra

ENV PYTHONPATH=/app/src

ENTRYPOINT ["chandra"]
CMD ["--help"]
