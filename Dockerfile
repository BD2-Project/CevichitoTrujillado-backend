# syntax=docker/dockerfile:1

FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS build
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.12-slim AS runtime
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
COPY data ./data
COPY scripts/generate_demo_audio.py ./scripts/generate_demo_audio.py
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    SOUPDB_HOST=soupdb \
    SOUPDB_PORT=55432 \
    COPYRIGHT_AUDIO_ROOT=/audio \
    COPYRIGHT_HOST=0.0.0.0 \
    COPYRIGHT_PORT=8000
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=8s --start-period=60s --retries=3 \
  CMD ["python", "-c", "import sys, urllib.request; sys.exit(urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health', timeout=5).status != 200)"]
CMD ["copyright-api"]
