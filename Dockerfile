# syntax=docker/dockerfile:1
# WISPMap collector image: small, non-root, multi-arch (linux/amd64 + linux/arm64).
#   docker build -t wispmap-collector .
# Runtime config comes from the environment (see .env.example); buffer + cached config live in /data.

ARG PYTHON_VERSION=3.12
ARG DEBIAN=trixie

FROM ghcr.io/astral-sh/uv:0.13.5 AS uv

# ---------- build: resolve and install into /app/.venv from the lock file ----------
FROM python:${PYTHON_VERSION}-slim-${DEBIAN} AS build
COPY --from=uv /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /src
# dependencies first: this layer is cached until uv.lock changes
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY README.md LICENSE ./
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

# ---------- runtime ----------
FROM python:${PYTHON_VERSION}-slim-${DEBIAN}
ARG VERSION=dev
LABEL org.opencontainers.image.title="wispmap-collector" \
      org.opencontainers.image.description="WISPMap collector: pings and polls MikroTik devices inside your network" \
      org.opencontainers.image.source="https://github.com/gigahost-za/wispmap-collector" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.version="${VERSION}"

# fping does all ICMP. Docker's default ping_group_range lets a non-root user send echo requests.
RUN apt-get update \
 && apt-get install -y --no-install-recommends fping \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --system --uid 10001 --home-dir /data --shell /usr/sbin/nologin wispmap \
 && mkdir -p /data && chown wispmap:wispmap /data

COPY --from=build /app/.venv /app/.venv
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    WISPMAP_BUFFER_DIR=/data

WORKDIR /data
USER wispmap
VOLUME ["/data"]
# exec form: the collector is PID 1 and gets SIGTERM from `docker stop` directly (it flushes its buffer to disk)
ENTRYPOINT ["wispmap-collector"]
