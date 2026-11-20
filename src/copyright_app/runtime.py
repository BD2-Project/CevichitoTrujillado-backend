"""El arranque comprueba que el catálogo y el HNSW ya existen en SoupDB."""

import logging
import sys
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import cache

from fastapi import FastAPI

from copyright_app.api.app import create_app
from copyright_app.errors import ServiceUnavailableError
from copyright_app.infrastructure.audio_store import SharedAudioStore
from copyright_app.infrastructure.repositories import (
    CATALOG_TABLES,
    HISTORY_TABLES,
    SoupDBCatalogRepository,
    SoupDBHistoryRepository,
    SoupDBSchema,
    SoupDBSearchRepository,
)
from copyright_app.infrastructure.segmentation import LibrosaSegmenter
from copyright_app.infrastructure.soupdb_client import (
    SoupDBClient,
    SoupDBClientError,
    SoupDBPool,
)
from copyright_app.schemas import SoupDBHealth
from copyright_app.services.analysis import CopyrightAnalyzer
from copyright_app.services.library import CatalogLibrary
from copyright_app.settings import ConfigError, Settings

logger = logging.getLogger(__name__)


def ping_soupdb(settings: Settings, timeout: float = 3.0) -> SoupDBHealth:
    """``PING``/``PONG`` por una conexión nueva (no ocupa el pool)."""
    started = time.perf_counter()
    try:
        with SoupDBClient(settings.soupdb_host, settings.soupdb_port, timeout=timeout) as client:
            client.ping()
    except SoupDBClientError:
        return SoupDBHealth(reachable=False)
    return SoupDBHealth(reachable=True, latency_ms=round((time.perf_counter() - started) * 1000, 3))


def build_app(settings: Settings) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        pool = SoupDBPool(
            lambda: SoupDBClient(
                settings.soupdb_host, settings.soupdb_port, timeout=settings.soupdb_timeout
            ),
            settings.pool_size,
        )
        try:
            if not ping_soupdb(settings).reachable:
                raise ServiceUnavailableError(
                    f"SoupDB no responde en {settings.soupdb_host}:{settings.soupdb_port}"
                )
            schema = SoupDBSchema(pool)
            missing = schema.missing_tables(CATALOG_TABLES)
            if missing:
                raise ServiceUnavailableError(
                    f"faltan tablas del catálogo {missing}; ejecuta copyright-bootstrap catalog"
                )
            search = SoupDBSearchRepository(pool)
            if not search.index_ready():
                raise ServiceUnavailableError(
                    "no existe el HNSW de copyright; ejecuta copyright-bootstrap catalog"
                )
            schema.ensure(HISTORY_TABLES)
            store = SharedAudioStore(
                settings.audio_root, max_upload_bytes=settings.max_upload_bytes
            )
            removed = store.clear_stale_queries()
            if removed:
                logger.info("borradas %d carpetas de candidatos huérfanas", removed)
            catalog = SoupDBCatalogRepository(pool)
            history = SoupDBHistoryRepository(pool)
            app.state.analyzer = CopyrightAnalyzer(
                store=store,
                segmenter=LibrosaSegmenter(),
                search=search,
                catalog=catalog,
                history=history,
                search_k=settings.search_k,
            )
            app.state.library = CatalogLibrary(catalog, store)
            app.state.history = history
            app.state.health_check = lambda: ping_soupdb(settings)
        except BaseException:
            pool.close()
            raise
        try:
            yield
        finally:
            app.state.analyzer = app.state.library = app.state.history = None
            pool.close()

    return create_app(
        lifespan=lifespan,
        cors_origins=settings.cors_origins,
        health_check=lambda: ping_soupdb(settings),
    )


@cache
def _app_from_env() -> FastAPI:
    return build_app(Settings.from_env())


def __getattr__(name: str) -> FastAPI:
    # ``uvicorn copyright_app.runtime:app``: el entorno se lee al pedir la app.
    if name == "app":
        return _app_from_env()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        sys.exit(f"configuración inválida: {exc}")
    import uvicorn

    uvicorn.run(build_app(settings), host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
