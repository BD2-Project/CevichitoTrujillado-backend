import logging
from collections.abc import Callable, Sequence
from contextlib import AbstractAsyncContextManager
from typing import TYPE_CHECKING, Annotated, Literal

from fastapi import APIRouter, FastAPI, File, HTTPException, Query, Request, UploadFile, status
from fastapi import Path as PathParam
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response

from copyright_app.config import API_PREFIX, SERVICE_NAME
from copyright_app.domain.models import SongEntry
from copyright_app.errors import InvalidAudioError, ServiceUnavailableError
from copyright_app.schemas import (
    AnalysisResult,
    AnalysisSummary,
    CatalogResponse,
    CatalogSegment,
    CatalogSong,
    CatalogSongDetail,
    ErrorResponse,
    HealthResponse,
    SoupDBHealth,
    StoredAnalysis,
)
from copyright_app.services.export import export_filename, to_csv, to_json

if TYPE_CHECKING:
    from copyright_app.domain.ports import HistoryRepository
    from copyright_app.services.analysis import CopyrightAnalyzer
    from copyright_app.services.library import CatalogLibrary

logger = logging.getLogger(__name__)

NOT_CONFIGURED = "El servicio no está configurado."
SONG_NOT_FOUND = "La canción no existe en el catálogo."
AUDIO_NOT_AVAILABLE = "El audio de la canción no está disponible."
ANALYSIS_NOT_FOUND = "El análisis no existe."
INTERNAL_ERROR = "Error interno del análisis."
CORS_METHODS = ("GET", "POST")
CORS_HEADERS = ("Accept", "Content-Type", "Range")
CORS_EXPOSE_HEADERS = ("Accept-Ranges", "Content-Disposition", "Content-Length", "Content-Range")
MAX_SONG_ID = 2**31 - 1

Lifespan = Callable[[FastAPI], AbstractAsyncContextManager[None]]
HealthCheck = Callable[[], SoupDBHealth]
SongId = Annotated[int, PathParam(gt=0, le=MAX_SONG_ID, description="song_id del catálogo.")]
_NOT_FOUND = {status.HTTP_404_NOT_FOUND: {"model": ErrorResponse}}
_UNAVAILABLE = {status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ErrorResponse}}


def audio_url(song_id: int) -> str:
    return f"{API_PREFIX}/catalog/{song_id}/audio"


def _catalog_song(entry: SongEntry) -> CatalogSong:
    song = entry.song
    return CatalogSong(
        song_id=song.song_id,
        title=song.title,
        artist=song.artist,
        duration_seconds=song.duration_seconds,
        segment_count=len(entry.segments),
        audio_url=audio_url(song.song_id),
    )


def _service(request: Request, name: str):
    service = getattr(request.app.state, name, None)
    if service is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, NOT_CONFIGURED)
    return service


def create_app(
    *,
    analyzer: "CopyrightAnalyzer | None" = None,
    library: "CatalogLibrary | None" = None,
    history: "HistoryRepository | None" = None,
    health_check: HealthCheck | None = None,
    lifespan: Lifespan | None = None,
    cors_origins: Sequence[str] = (),
) -> FastAPI:
    app = FastAPI(
        lifespan=lifespan,
        title=SERVICE_NAME,
        description=(
            "API de análisis de similitud acústica y temporal. Usa SoupDB por TCP "
            "(protocolo v2). Devuelve evidencia de similitud; no determina copyright."
        ),
        version="1.0.0",
    )
    app.state.analyzer = analyzer
    app.state.library = library
    app.state.history = history
    app.state.health_check = health_check
    if cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(cors_origins),
            allow_methods=list(CORS_METHODS),
            allow_headers=list(CORS_HEADERS),
            expose_headers=list(CORS_EXPOSE_HEADERS),
            allow_credentials=False,
        )
    router = APIRouter(prefix=API_PREFIX)

    @app.exception_handler(ServiceUnavailableError)
    async def _unavailable(_request: Request, exc: ServiceUnavailableError) -> JSONResponse:
        logger.warning("servicio no disponible: %s", exc, exc_info=exc.__cause__)
        return JSONResponse(status_code=503, content={"detail": str(exc)})

    @app.exception_handler(InvalidAudioError)
    async def _invalid(_request: Request, exc: InvalidAudioError) -> JSONResponse:
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    @app.exception_handler(Exception)
    async def _unexpected(_request: Request, exc: Exception) -> JSONResponse:
        logger.exception("error inesperado en la API de copyright", exc_info=exc)
        return JSONResponse(status_code=500, content={"detail": INTERNAL_ERROR})

    @router.get(
        "/health",
        response_model=HealthResponse,
        tags=["health"],
        responses={503: {"model": HealthResponse, "description": "SoupDB no responde."}},
    )
    def health(request: Request) -> JSONResponse:
        check: HealthCheck | None = request.app.state.health_check
        soupdb = check() if check is not None else SoupDBHealth(reachable=False)
        body = HealthResponse(status="ok" if soupdb.reachable else "unavailable", soupdb=soupdb)
        return JSONResponse(status_code=200 if soupdb.reachable else 503, content=body.model_dump())

    @router.post(
        "/analyze",
        response_model=StoredAnalysis | AnalysisResult,
        tags=["analysis"],
        summary="Analizar la similitud de un audio con el catálogo",
        responses={
            422: {"description": "Falta `file`, o el audio está vacío o no se decodifica."},
            503: {"model": ErrorResponse, "description": "SoupDB o el índice no disponible."},
            500: {"model": ErrorResponse, "description": "Error interno inesperado."},
        },
    )
    def analyze(
        request: Request,
        file: UploadFile = File(description="Archivo de audio a analizar."),  # noqa: B008
    ) -> AnalysisResult:
        analyzer: CopyrightAnalyzer = _service(request, "analyzer")
        return analyzer.analyze(file.file, file.filename)

    @router.get(
        "/catalog",
        response_model=CatalogResponse,
        tags=["catalog"],
        summary="Canciones originales del catálogo (solo lectura)",
        responses=_UNAVAILABLE,
    )
    def catalog(request: Request) -> CatalogResponse:
        library: CatalogLibrary = _service(request, "library")
        songs = [_catalog_song(entry) for entry in library.list_songs()]
        return CatalogResponse(
            song_count=len(songs),
            segment_count=sum(song.segment_count for song in songs),
            songs=songs,
        )

    @router.get(
        "/catalog/{song_id}",
        response_model=CatalogSongDetail,
        tags=["catalog"],
        summary="Detalle de una canción y sus segmentos indexados",
        responses={**_NOT_FOUND, **_UNAVAILABLE},
    )
    def catalog_song(request: Request, song_id: SongId) -> CatalogSongDetail:
        library: CatalogLibrary = _service(request, "library")
        entry = library.get_song(song_id)
        if entry is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, SONG_NOT_FOUND)
        return CatalogSongDetail(
            **_catalog_song(entry).model_dump(),
            segments=[
                CatalogSegment(
                    segment_id=s.segment_id,
                    segment_index=s.segment_index,
                    start_seconds=s.start_seconds,
                    end_seconds=s.end_seconds,
                )
                for s in entry.segments
            ],
        )

    @router.get(
        "/catalog/{song_id}/audio",
        response_class=FileResponse,
        tags=["catalog"],
        summary="Audio original de una canción catalogada (admite Range)",
        responses={
            200: {"content": {"audio/mpeg": {}, "audio/wav": {}}},
            206: {"description": "Contenido parcial (cabecera Range)."},
            **_NOT_FOUND,
            **_UNAVAILABLE,
        },
    )
    def catalog_song_audio(request: Request, song_id: SongId) -> FileResponse:
        library: CatalogLibrary = _service(request, "library")
        audio = library.reference_audio(song_id)
        if audio is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, AUDIO_NOT_AVAILABLE)
        return FileResponse(
            audio.path,
            media_type=audio.media_type,
            filename=audio.filename,
            content_disposition_type="inline",
        )

    def get_stored(request: Request, analysis_id: str) -> StoredAnalysis:
        history: HistoryRepository = _service(request, "history")
        stored = history.get(analysis_id)
        if stored is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, ANALYSIS_NOT_FOUND)
        return stored

    @router.get(
        "/analyses",
        response_model=list[AnalysisSummary],
        tags=["history"],
        summary="Análisis guardados, del más reciente al más antiguo",
        responses=_UNAVAILABLE,
    )
    def analyses(request: Request) -> list[AnalysisSummary]:
        history: HistoryRepository = _service(request, "history")
        return history.list_summaries()

    @router.get(
        "/analyses/{analysis_id}",
        response_model=StoredAnalysis,
        tags=["history"],
        summary="Resultado completo de un análisis guardado (sin el audio subido)",
        responses={**_NOT_FOUND, **_UNAVAILABLE},
    )
    def analysis_detail(request: Request, analysis_id: str) -> StoredAnalysis:
        return get_stored(request, analysis_id)

    @router.get(
        "/analyses/{analysis_id}/export",
        tags=["history"],
        summary="Descargar un análisis guardado en JSON o CSV",
        responses={
            200: {"content": {"application/json": {}, "text/csv": {}}},
            **_NOT_FOUND,
            **_UNAVAILABLE,
        },
    )
    def analysis_export(
        request: Request,
        analysis_id: str,
        format: Annotated[Literal["json", "csv"], Query(description="json o csv")] = "json",  # noqa: A002
    ) -> Response:
        stored = get_stored(request, analysis_id)
        if format == "csv":
            body, media_type = to_csv(stored), "text/csv; charset=utf-8"
        else:
            body, media_type = to_json(stored), "application/json"
        return Response(
            content=body,
            media_type=media_type,
            headers={
                "Content-Disposition": (
                    f'attachment; filename="{export_filename(stored.analysis_id, format)}"'
                )
            },
        )

    app.include_router(router)
    return app
