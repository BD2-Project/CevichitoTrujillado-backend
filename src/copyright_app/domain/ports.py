"""Interfaces que el dominio necesita; las implementa ``infrastructure``.

El dominio y los servicios dependen solo de estos protocolos: se prueban con
dobles en memoria, sin SoupDB. Las implementaciones reales
(``infrastructure.repositories``) hablan con SoupDB únicamente por TCP.
"""

from collections.abc import Sequence
from contextlib import AbstractContextManager
from pathlib import Path
from typing import BinaryIO, Protocol

from copyright_app.domain.models import NeighborRow, SegmentInterval, Song, SongEntry
from copyright_app.schemas import AnalysisResult, AnalysisSummary, StoredAnalysis


class CatalogRepository(Protocol):
    """Canciones originales (``songs``/``audio_segments``)."""

    def list_songs(self) -> list[SongEntry]: ...

    def get_song(self, song_id: int) -> SongEntry | None: ...

    def songs_by_id(self, song_ids: Sequence[int]) -> dict[int, Song]: ...


class SearchRepository(Protocol):
    """Búsqueda vectorial de un segmento ya escrito en el volumen compartido."""

    def similar_segments(self, reference: str, k: int) -> list[NeighborRow]: ...

    def index_ready(self) -> bool: ...


class HistoryRepository(Protocol):
    """Análisis guardados (``analyses``/``analysis_matches``)."""

    def save(self, result: AnalysisResult) -> StoredAnalysis: ...

    def list_summaries(self) -> list[AnalysisSummary]: ...

    def get(self, analysis_id: str) -> StoredAnalysis | None: ...


class AudioStore(Protocol):
    """Volumen ``/audio`` compartido con SoupDB."""

    def stage(
        self, analysis_id: str, extension: str, upload: BinaryIO
    ) -> AbstractContextManager[tuple[str, Path]]:
        """Escribe el candidato; produce ``(referencia relativa, ruta local)`` y lo borra."""
        ...

    def reference_file(self, audio_path: str) -> Path | None: ...


class Segmenter(Protocol):
    """Calcula los bloques de 5 s del candidato (no extrae MFCC)."""

    def intervals(self, path: Path) -> list[SegmentInterval]: ...
