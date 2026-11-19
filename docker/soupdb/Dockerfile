# syntax=docker/dockerfile:1
# Servidor SoupDB (gestor + plugin soupdb-vector) construido desde el repositorio de
# SoupDB sin modificarlo. El contexto "soupdb" lo da docker-compose.yml
# (additional_contexts → ${SOUPDB_SOURCE:-../SoupBD}). Este Dockerfile vive aquí solo
# para el despliegue de la aplicación; el backend copyright no se construye con él.

FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS build
WORKDIR /opt/soupdb
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY --from=soupdb pyproject.toml uv.lock README.md ./
COPY --from=soupdb plugins ./plugins
COPY --from=soupdb engine ./engine
# Versiones del uv.lock de SoupDB: gestor (soupdb) + soupdb-vector y sus dependencias.
RUN uv sync --frozen --no-dev --no-editable --package soupdb-vector

FROM python:3.12-slim AS runtime
WORKDIR /opt/soupdb
COPY --from=build /opt/soupdb/.venv /opt/soupdb/.venv
ENV PATH="/opt/soupdb/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    SOUPDB_DATA=/data/db \
    SOUPDB_PLUGIN_STORAGE=/data/plugins \
    DRIVER_HOST=0.0.0.0 \
    DRIVER_PORT=55432
EXPOSE 55432
# PING/PONG del protocolo v2 (frame de 8 bytes "SP" 0x02 0x01 + longitud 0).
HEALTHCHECK --interval=10s --timeout=5s --start-period=30s --retries=5 \
  CMD ["python", "-c", "import socket; s = socket.create_connection(('127.0.0.1', 55432), 3); s.sendall(b'SP\\x02\\x01\\x00\\x00\\x00\\x00'); assert s.recv(8)[:4] == b'SP\\x02\\x02'"]
CMD ["python", "-m", "engine"]
