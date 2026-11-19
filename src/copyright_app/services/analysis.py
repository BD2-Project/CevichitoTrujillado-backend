"""Análisis de un upload: staging compartido → segmentos → HNSW remoto → evidencia → historial.

Depende solo de los puertos del dominio (``domain.ports``):

1. ``AudioStore.stage``: escribe el upload en ``queries/<analysis_id>/candidate<ext>``
   del volumen compartido con SoupDB; ``analysis_id`` es un ``uuid4`` generado aquí.
2. ``Segmenter.intervals``: bloques de 5 s (misma regla que el perfil de SoupDB).
3. ``SearchRepository.similar_segments`` por segmento: SoupDB vectoriza
   ``queries/<id>/candidate<ext>#segment=NNNNNN`` y busca en su HNSW.
4. ``build_temporal_evidence`` (puro) → ``AnalysisResult``.
5. ``HistoryRepository.save`` si hay historial.
6. El candidato se borra siempre (``finally`` del staging).

El resultado es evidencia de similitud acústica y temporal; no determina copyright.
"""

import re
import uuid
from pathlib import PurePosixPath, PureWindowsPath
from typing import BinaryIO

from copyright_app.config import AUDIO_EXTENSIONS, MIN_SEGMENT_RATIO, SEGMENT_SECONDS
from copyright_app.domain.evidence import TemporalEvidenceResult, build_temporal_evidence
from copyright_app.domain.models import (
    AudioSearchResult,
    QuerySegmentResult,
    SegmentMatch,
    segment_audio_ref,
)
from copyright_app.domain.ports import (
    AudioStore,
    CatalogRepository,
    HistoryRepository,
    SearchRepository,
    Segmenter,
)
from copyright_app.errors import InvalidAudioError, RepositoryError
from copyright_app.schemas import AnalysisResult, CopyrightMatch

DEFAULT_SEARCH_K = 5
_FALLBACK_FILENAME = "upload"
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


def display_filename(raw: str | None) -> str:
    """Solo el último componente del nombre del cliente; nunca decide la ruta."""
    name = _CONTROL_CHARS.sub("", PureWindowsPath(raw or "").name).strip()
    if name in ("", ".", ".."):
        return _FALLBACK_FILENAME
    return name[:255]


class CopyrightAnalyzer:
    def __init__(
        self,
        *,
        store: AudioStore,
        segmenter: Segmenter,
        search: SearchRepository,
        catalog: CatalogRepository,
        history: HistoryRepository | None = None,
        search_k: int = DEFAULT_SEARCH_K,
        new_id=lambda: uuid.uuid4().hex,
    ) -> None:
        if type(search_k) is not int or search_k <= 0:
            raise ValueError(f"search_k debe ser un entero positivo, no {search_k!r}")
        self._store = store
        self._segmenter = segmenter
        self._search = search
        self._catalog = catalog
        self._history = history
        self._search_k = search_k
        self._new_id = new_id

    @property
    def search_k(self) -> int:
        return self._search_k

    def analyze(self, upload: BinaryIO, filename: str | None) -> AnalysisResult:
        analysis_id = self._new_id()
        name = display_filename(filename)
        extension = PurePosixPath(name).suffix.lower()
        if extension not in AUDIO_EXTENSIONS:
            extension = ""
        with self._store.stage(analysis_id, extension, upload) as (reference, local_path):
            intervals = self._segmenter.intervals(local_path)
            if not intervals:
                raise InvalidAudioError(
                    "el audio es demasiado corto (se necesita al menos un segmento de "
                    f"{SEGMENT_SECONDS * MIN_SEGMENT_RATIO:g} s)"
                )
            neighbors = [
                (
                    interval,
                    self._search.similar_segments(
                        segment_audio_ref(reference, interval.index), self._search_k
                    ),
                )
                for interval in intervals
            ]
        songs = self._catalog.songs_by_id([row.song_id for _, rows in neighbors for row in rows])
        segments = []
        for interval, rows in neighbors:
            matches = []
            for row in rows:
                song = songs.get(row.song_id)
                if song is None:
                    raise RepositoryError(
                        f"catálogo inconsistente: el segmento {row.segment_id} apunta a "
                        f"song_id={row.song_id}, que no existe"
                    )
                matches.append(
                    SegmentMatch(
                        segment_id=row.segment_id,
                        song_id=row.song_id,
                        title=song.title,
                        artist=song.artist,
                        source_segment_index=row.segment_index,
                        source_start_seconds=row.start_seconds,
                        source_end_seconds=row.end_seconds,
                        audio_ref=row.audio_ref,
                        score=row.score,
                    )
                )
            segments.append(
                QuerySegmentResult(
                    query_segment_index=interval.index,
                    query_start_seconds=interval.start_seconds,
                    query_end_seconds=interval.end_seconds,
                    matches=tuple(matches),
                )
            )
        evidence = build_temporal_evidence(
            AudioSearchResult(audio_path=reference, k=self._search_k, segments=tuple(segments))
        )
        result = to_analysis_result(analysis_id, name, evidence)
        if self._history is not None:
            return self._history.save(result)
        return result


def to_analysis_result(
    analysis_id: str, filename: str, evidence: TemporalEvidenceResult
) -> AnalysisResult:
    """Una ``CopyrightMatch`` por ``TemporalRun``, en el orden de la evidencia."""
    return AnalysisResult(
        analysis_id=analysis_id,
        filename=filename,
        segment_seconds=SEGMENT_SECONDS,
        matches=[
            CopyrightMatch(
                query_start=run.query_start_seconds,
                query_end=run.query_end_seconds,
                song_id=run.song_id,
                title=run.title,
                artist=run.artist,
                source_start=run.source_start_seconds,
                source_end=run.source_end_seconds,
                score=run.mean_score,
                segment_count=run.segment_count,
                min_score=run.min_score,
                max_score=run.max_score,
                alignment_offset_seconds=run.alignment_offset_seconds,
                # Identidad: la misma run que eligió build_temporal_evidence.
                is_best_run=run is song.best_run,
            )
            for song in evidence.songs
            for run in song.runs
        ],
    )
