"""Repositorios sobre SoupDB que solo usan :class:`SoupDBClient` (TCP, protocolo v2).

Todo es SQL enviado al servidor. Los valores van como parámetros de prepared
statements (``$1..$n``); la única excepción es la referencia de ``SIMILAR_TO``,
que el parser de SoupDB solo admite como literal: se genera en servidor y se
valida con una expresión regular estricta antes de escribirla en el SQL.

SoupDB no tiene ``UNIQUE``, ``FOREIGN KEY`` ni ``IF NOT EXISTS``: las invariantes
(ids únicos, padre existente, ``segment_index`` único por canción, ``audio_ref``
canónico) se comprueban con consultas antes de insertar, dentro de una
transacción ``BEGIN``/``COMMIT`` de la conexión. La existencia y el schema de
las tablas se consultan en las tablas de sistema (``SysTables``, ``SysColumns``,
``SysIndexes``, ``SysPluginIndexes``) con ``SELECT``.
"""

from __future__ import annotations

import json
import logging
import math
import re
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime

from copyright_app.config import (
    ANALYSES,
    ANALYSIS_MATCHES,
    AUDIO_SEGMENTS,
    INDEX_KIND,
    INDEX_NAME,
    INDEXED_COLUMN,
    METRIC,
    PROFILE_ID,
    SONGS,
    VECTOR_DIMENSION,
)
from copyright_app.domain.models import (
    CANDIDATE_REF,
    SEGMENT_MARKER,
    AudioSegment,
    NeighborRow,
    Song,
    SongEntry,
    segment_audio_ref,
)
from copyright_app.errors import (
    IncompatibleSchemaError,
    IntegrityError,
    InvalidAudioError,
    RepositoryError,
    ServiceUnavailableError,
)
from copyright_app.infrastructure.soupdb_client import (
    SoupDBClient,
    SoupDBClientError,
    SoupDBError,
    SoupDBPool,
)
from copyright_app.schemas import (
    AnalysisResult,
    AnalysisSummary,
    CopyrightMatch,
    StoredAnalysis,
)

logger = logging.getLogger(__name__)

#: ``(columna, tipo SQL)`` de cada tabla de la aplicación.
TABLES: dict[str, tuple[tuple[str, str], ...]] = {
    SONGS: (
        ("song_id", "INT"),
        ("title", "TEXT"),
        ("artist", "TEXT"),
        ("audio_path", "TEXT"),
        ("duration_seconds", "FLOAT"),
    ),
    AUDIO_SEGMENTS: (
        ("segment_id", "INT"),
        ("song_id", "INT"),
        ("segment_index", "INT"),
        ("start_seconds", "FLOAT"),
        ("end_seconds", "FLOAT"),
        ("audio_ref", "TEXT"),
    ),
    ANALYSES: (
        ("analysis_id", "TEXT"),
        ("filename", "TEXT"),
        ("created_at", "TEXT"),
        ("segment_seconds", "FLOAT"),
        ("match_count", "INT"),
    ),
    ANALYSIS_MATCHES: (
        ("analysis_id", "TEXT"),
        ("match_order", "INT"),
        ("song_id", "INT"),
        ("query_start", "FLOAT"),
        ("query_end", "FLOAT"),
        ("source_start", "FLOAT"),
        ("source_end", "FLOAT"),
        ("score", "FLOAT"),
        ("segment_count", "INT"),
        ("min_score", "FLOAT"),
        ("max_score", "FLOAT"),
        ("alignment_offset_seconds", "FLOAT"),
        ("is_best_run", "BOOL"),
    ),
}
#: Índices B+ (no son restricciones de unicidad).
BTREE_INDEXES: dict[str, tuple[str, ...]] = {
    SONGS: ("song_id",),
    AUDIO_SEGMENTS: ("segment_id", "song_id"),
    ANALYSES: ("analysis_id",),
    ANALYSIS_MATCHES: ("analysis_id",),
}
CATALOG_TABLES = (SONGS, AUDIO_SEGMENTS)
HISTORY_TABLES = (ANALYSES, ANALYSIS_MATCHES)

ANALYSIS_ID = re.compile(r"[0-9a-f]{32}")
_SEGMENT_REF = re.compile(r"#segment=[0-9]{6}$")
MAX_K = 1000
_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"


def utc_now() -> str:
    return datetime.now(UTC).strftime(_TIMESTAMP_FORMAT)


def text_literal(value: str) -> str:
    """Literal de texto SQL (comillas simples, ``'`` duplicada)."""
    return "'" + value.replace("'", "''") + "'"


class _Base:
    def __init__(self, pool: SoupDBPool) -> None:
        self._pool = pool

    @contextmanager
    def _connection(self) -> Iterator[SoupDBClient]:
        try:
            with self._pool.connection() as client:
                yield client
        except SoupDBError:
            raise
        except SoupDBClientError as exc:
            raise ServiceUnavailableError("SoupDB no está disponible") from exc


# --- schema -------------------------------------------------------------------------


class SoupDBSchema(_Base):
    """Crea y valida tablas e índices B+ por SQL. Idempotente; nunca borra datos."""

    def ensure(self, tables: Sequence[str]) -> None:
        with self._connection() as db:
            existing = {row[0] for row in db.query("SELECT table_name FROM SysTables").rows}
            for table in tables:
                expected = TABLES[table]
                if table in existing:
                    actual = self.columns(db, table)
                    if actual != expected:
                        raise IncompatibleSchemaError(
                            f"la tabla {table!r} existe con otro schema: {actual}"
                        )
                else:
                    columns = ", ".join(f"{name} {kind}" for name, kind in expected)
                    db.query(f"CREATE TABLE {table} ({columns})")
                indexed = {
                    row[0]
                    for row in db.run(
                        "SELECT column_name FROM SysIndexes WHERE table_name = $1", (table,)
                    ).rows
                }
                for column in BTREE_INDEXES[table]:
                    if column not in indexed:
                        db.query(f"CREATE INDEX {table}_{column} ON {table} ({column}) TYPE BTREE")

    @staticmethod
    def columns(db: SoupDBClient, table: str) -> tuple[tuple[str, str], ...]:
        rows = db.run(
            "SELECT position, column_name, type_name FROM SysColumns WHERE table_name = $1",
            (table,),
        ).rows
        return tuple((name, str(kind).upper()) for _pos, name, kind in sorted(rows))

    def missing_tables(self, tables: Sequence[str]) -> list[str]:
        with self._connection() as db:
            existing = {row[0] for row in db.query("SELECT table_name FROM SysTables").rows}
        return [table for table in tables if table not in existing]


# --- catálogo ------------------------------------------------------------------------


def _song(row: Sequence[object]) -> Song:
    return Song(*row)  # type: ignore[arg-type]


def _segment(row: Sequence[object]) -> AudioSegment:
    return AudioSegment(*row)  # type: ignore[arg-type]


class SoupDBCatalogRepository(_Base):
    """``songs``/``audio_segments``: lectura para el API y escritura para el bootstrap."""

    def list_songs(self) -> list[SongEntry]:
        with self._connection() as db:
            songs = [_song(r) for r in db.query(f"SELECT * FROM {SONGS} ORDER BY song_id").rows]
            segments: dict[int, list[AudioSegment]] = {}
            for row in db.query(
                f"SELECT * FROM {AUDIO_SEGMENTS} ORDER BY song_id, segment_index"
            ).rows:
                segment = _segment(row)
                segments.setdefault(segment.song_id, []).append(segment)
        return [SongEntry(s, tuple(segments.get(s.song_id, ()))) for s in songs]

    def get_song(self, song_id: int) -> SongEntry | None:
        if type(song_id) is not int or not 0 < song_id < 2**31:
            return None
        with self._connection() as db:
            song = self._get_song(db, song_id)
            if song is None:
                return None
            rows = db.run(
                f"SELECT * FROM {AUDIO_SEGMENTS} WHERE song_id = $1 ORDER BY segment_index",
                (song_id,),
            ).rows
        return SongEntry(song, tuple(_segment(r) for r in rows))

    def songs_by_id(self, song_ids: Sequence[int]) -> dict[int, Song]:
        found: dict[int, Song] = {}
        with self._connection() as db:
            for song_id in dict.fromkeys(song_ids):
                song = self._get_song(db, song_id)
                if song is not None:
                    found[song_id] = song
        return found

    def counts(self) -> tuple[int, int]:
        with self._connection() as db:
            songs = len(db.query(f"SELECT song_id FROM {SONGS}").rows)
            segments = len(db.query(f"SELECT segment_id FROM {AUDIO_SEGMENTS}").rows)
        return songs, segments

    def register_song(self, song: Song, segments: Sequence[AudioSegment]) -> bool:
        """Registra una canción y sus segmentos en una transacción.

        Si la canción ya existe **idéntica** (mismos campos y segmentos) no hace
        nada y devuelve ``False``; si existe distinta, :class:`IntegrityError`.
        """
        self._validate_segments(song, segments)
        with self._connection() as db:
            existing = self._get_song(db, song.song_id)
            if existing is not None:
                stored = tuple(
                    _segment(r)
                    for r in db.run(
                        f"SELECT * FROM {AUDIO_SEGMENTS} WHERE song_id = $1 ORDER BY segment_index",
                        (song.song_id,),
                    ).rows
                )
                if existing != song or stored != tuple(segments):
                    raise IntegrityError(f"song_id {song.song_id} ya existe con otros datos")
                return False
            with db.transaction():
                if db.run(
                    f"SELECT song_id FROM {SONGS} WHERE audio_path = $1", (song.audio_path,)
                ).rows:
                    raise IntegrityError(f"audio_path {song.audio_path!r} ya está registrado")
                for segment in segments:
                    if db.run(
                        f"SELECT segment_id FROM {AUDIO_SEGMENTS} WHERE segment_id = $1",
                        (segment.segment_id,),
                    ).rows:
                        raise IntegrityError(f"segment_id {segment.segment_id} ya existe")
                db.run(
                    f"INSERT INTO {SONGS} VALUES ($1, $2, $3, $4, $5)",
                    (song.song_id, song.title, song.artist, song.audio_path, song.duration_seconds),
                )
                for s in segments:
                    db.run(
                        f"INSERT INTO {AUDIO_SEGMENTS} VALUES ($1, $2, $3, $4, $5, $6)",
                        (
                            s.segment_id,
                            s.song_id,
                            s.segment_index,
                            s.start_seconds,
                            s.end_seconds,
                            s.audio_ref,
                        ),
                    )
        return True

    @staticmethod
    def _validate_segments(song: Song, segments: Sequence[AudioSegment]) -> None:
        if not segments:
            raise IntegrityError(f"la canción {song.song_id} no tiene segmentos")
        seen_ids: set[int] = set()
        for expected_index, segment in enumerate(segments):
            if segment.song_id != song.song_id:
                raise IntegrityError(f"segmento {segment.segment_id} de otra canción")
            if segment.segment_index != expected_index:
                raise IntegrityError(
                    f"la canción {song.song_id} debe tener segment_index 0..n-1 consecutivos"
                )
            if segment.audio_ref != segment_audio_ref(song.audio_path, segment.segment_index):
                raise IntegrityError(f"audio_ref no canónico: {segment.audio_ref!r}")
            if segment.segment_id in seen_ids:
                raise IntegrityError(f"segment_id {segment.segment_id} repetido")
            seen_ids.add(segment.segment_id)

    @staticmethod
    def _get_song(db: SoupDBClient, song_id: int) -> Song | None:
        rows = db.run(f"SELECT * FROM {SONGS} WHERE song_id = $1", (song_id,)).rows
        if not rows:
            return None
        if len(rows) > 1:
            raise IntegrityError(f"song_id {song_id} está duplicado en la base")
        return _song(rows[0])


# --- búsqueda HNSW -----------------------------------------------------------------------

_SIMILAR_SQL = (
    "SELECT segment_id, song_id, segment_index, start_seconds, end_seconds, audio_ref, "
    "SCORE() AS score FROM {table} WHERE SIMILAR_TO({reference}, k={k}) "
    "USING {kind} WITH METRIC={metric} ORDER BY score DESC"
)


class SoupDBSearchRepository(_Base):
    """``SIMILAR_TO`` remoto: SoupDB vectoriza el segmento (MFCC→TF-IDF) y busca en HNSW."""

    def similar_segments(self, reference: str, k: int) -> list[NeighborRow]:
        path, marker, _ = reference.partition(SEGMENT_MARKER)
        if not marker or not CANDIDATE_REF.fullmatch(path) or not _SEGMENT_REF.search(reference):
            raise ValueError(f"referencia de candidato inválida: {reference!r}")
        if type(k) is not int or not 0 < k <= MAX_K:
            raise ValueError(f"k debe ser un entero entre 1 y {MAX_K}")
        sql = _SIMILAR_SQL.format(
            table=AUDIO_SEGMENTS,
            reference=text_literal(reference),
            k=k,
            kind=INDEX_KIND,
            metric=METRIC,
        )
        try:
            with self._connection() as db:
                rows = db.query(sql).rows
        except SoupDBError as exc:
            if "vectorize" in exc.message:
                raise InvalidAudioError(
                    "SoupDB no pudo extraer descriptores de un segmento del audio"
                ) from exc
            raise ServiceUnavailableError("el índice de copyright no está disponible") from exc
        return [NeighborRow(*row) for row in rows]  # type: ignore[arg-type]

    def index_ready(self) -> bool:
        """El HNSW existe en ``SysPluginIndexes`` con la configuración acordada."""
        try:
            with self._connection() as db:
                rows = db.run(
                    "SELECT table_name, column_name, index_type, options_json "
                    "FROM SysPluginIndexes WHERE index_name = $1",
                    (INDEX_NAME,),
                ).rows
        except SoupDBError:
            return False
        if len(rows) != 1:
            return False
        table, column, kind, options_json = rows[0]
        try:
            options = json.loads(options_json or "{}")
        except ValueError:
            return False
        return (
            (table, column, str(kind).upper()) == (AUDIO_SEGMENTS, INDEXED_COLUMN, INDEX_KIND)
            and str(options.get("metric", "")).lower() == METRIC
            and int(options.get("dimension", 0)) == VECTOR_DIMENSION
            and options.get("profile_id") == PROFILE_ID
        )


# --- historial -------------------------------------------------------------------------------


class SoupDBHistoryRepository(_Base):
    """``analyses``/``analysis_matches``. Solo resultado y metadata, nunca el audio."""

    def __init__(self, pool: SoupDBPool, *, clock: Callable[[], str] = utc_now) -> None:
        super().__init__(pool)
        self._clock = clock

    def save(self, result: AnalysisResult) -> StoredAnalysis:
        """Guarda el análisis en una transacción (runs y cabecera) y lo devuelve."""
        if not ANALYSIS_ID.fullmatch(result.analysis_id):
            raise IntegrityError(f"analysis_id inválido: {result.analysis_id!r}")
        created_at = self._clock()
        with self._connection() as db:
            with db.transaction():
                if self._header(db, result.analysis_id) is not None:
                    raise IntegrityError(f"el análisis {result.analysis_id} ya existe")
                for order, match in enumerate(result.matches):
                    db.run(
                        f"INSERT INTO {ANALYSIS_MATCHES} VALUES "
                        "($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)",
                        _match_params(result.analysis_id, order, match),
                    )
                # La cabecera va al final: las lecturas solo ven análisis completos.
                db.run(
                    f"INSERT INTO {ANALYSES} VALUES ($1, $2, $3, $4, $5)",
                    (
                        result.analysis_id,
                        result.filename,
                        created_at,
                        float(result.segment_seconds),
                        len(result.matches),
                    ),
                )
        return StoredAnalysis(**result.model_dump(), created_at=created_at)

    def list_summaries(self) -> list[AnalysisSummary]:
        with self._connection() as db:
            headers = db.query(
                f"SELECT * FROM {ANALYSES} ORDER BY created_at DESC, analysis_id DESC"
            ).rows
            matches: dict[str, list[tuple]] = {}
            for row in db.query(f"SELECT analysis_id, song_id, score FROM {ANALYSIS_MATCHES}").rows:
                matches.setdefault(str(row[0]), []).append(row)
        summaries = []
        for analysis_id, filename, created_at, segment_seconds, match_count in headers:
            rows = matches.get(str(analysis_id), [])
            if len(rows) != match_count:
                raise RepositoryError(
                    f"el análisis {analysis_id} tiene {len(rows)} runs; se esperaban {match_count}"
                )
            summaries.append(
                AnalysisSummary(
                    analysis_id=analysis_id,
                    filename=filename,
                    created_at=created_at,
                    segment_seconds=segment_seconds,
                    match_count=match_count,
                    song_count=len({row[1] for row in rows}),
                    best_score=max((row[2] for row in rows), default=None),
                )
            )
        return summaries

    def get(self, analysis_id: str) -> StoredAnalysis | None:
        if not ANALYSIS_ID.fullmatch(analysis_id):
            return None
        with self._connection() as db:
            header = self._header(db, analysis_id)
            if header is None:
                return None
            rows = db.run(
                f"SELECT * FROM {ANALYSIS_MATCHES} WHERE analysis_id = $1 ORDER BY match_order",
                (analysis_id,),
            ).rows
            _id, filename, created_at, segment_seconds, match_count = header
            if len(rows) != match_count:
                raise RepositoryError(f"el análisis {analysis_id} está incompleto")
            songs: dict[int, Song] = {}
            for row in rows:
                song_id = row[2]
                if song_id not in songs:
                    found = SoupDBCatalogRepository._get_song(db, song_id)
                    if found is None:
                        raise RepositoryError(
                            f"el análisis {analysis_id} referencia la canción {song_id}"
                        )
                    songs[song_id] = found
        matches = []
        for order, row in enumerate(rows):
            if row[1] != order:
                raise RepositoryError(f"orden de runs inconsistente en {analysis_id}")
            song = songs[row[2]]
            matches.append(_match_from_row(row, song.title, song.artist))
        return StoredAnalysis(
            analysis_id=analysis_id,
            filename=filename,
            created_at=created_at,
            segment_seconds=segment_seconds,
            matches=matches,
        )

    @staticmethod
    def _header(db: SoupDBClient, analysis_id: str) -> tuple | None:
        rows = db.run(f"SELECT * FROM {ANALYSES} WHERE analysis_id = $1", (analysis_id,)).rows
        if not rows:
            return None
        if len(rows) > 1:
            raise IntegrityError(f"analysis_id {analysis_id} está duplicado")
        return rows[0]


def _finite(value: float) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise IntegrityError(f"valor no finito: {value!r}")
    return number


def _match_params(analysis_id: str, order: int, match: CopyrightMatch) -> tuple:
    return (
        analysis_id,
        order,
        match.song_id,
        _finite(match.query_start),
        _finite(match.query_end),
        _finite(match.source_start),
        _finite(match.source_end),
        _finite(match.score),
        match.segment_count,
        _finite(match.min_score),
        _finite(match.max_score),
        _finite(match.alignment_offset_seconds),
        bool(match.is_best_run),
    )


def _match_from_row(row: Sequence[object], title: str, artist: str) -> CopyrightMatch:
    (
        _analysis_id,
        _order,
        song_id,
        query_start,
        query_end,
        source_start,
        source_end,
        score,
        segment_count,
        min_score,
        max_score,
        offset,
        is_best_run,
    ) = row
    return CopyrightMatch(
        query_start=query_start,
        query_end=query_end,
        song_id=song_id,
        title=title,
        artist=artist,
        source_start=source_start,
        source_end=source_end,
        score=score,
        segment_count=segment_count,
        min_score=min_score,
        max_score=max_score,
        alignment_offset_seconds=offset,
        is_best_run=is_best_run,
    )
