"""Modelos del dominio: catálogo, búsqueda por segmentos y referencias de audio.

Sin I/O. Las invariantes de cada fila se validan al construirla; las que
dependen de otras filas (ids únicos, padre existente) las comprueba el
repositorio antes de escribir.
"""

import math
import re
from dataclasses import dataclass
from pathlib import PurePosixPath, PureWindowsPath

from copyright_app.config import AUDIO_EXTENSIONS, QUERIES_DIR

SEGMENT_MARKER = "#segment="
MAX_ID = 2**31 - 1
#: Referencia de un candidato: generada en servidor, nunca por el cliente.
CANDIDATE_REF = re.compile(rf"^{QUERIES_DIR}/[0-9a-f]{{32}}/candidate(\.(wav|mp3|flac|ogg|au))?$")


def segment_audio_ref(audio_path: str, segment_index: int) -> str:
    """``<audio_path>#segment=NNNNNN``: la convención del perfil ``audio_segment``."""
    if type(segment_index) is not int or segment_index < 0:
        raise ValueError(f"segment_index inválido: {segment_index!r}")
    return f"{audio_path}{SEGMENT_MARKER}{segment_index:06d}"


def is_relative_audio_path(path: str) -> bool:
    """Ruta relativa POSIX canónica, sin ``..``/``.``, ``\\`` ni ``#``."""
    if not isinstance(path, str) or not path or path != path.strip():
        return False
    posix = PurePosixPath(path)
    return not (
        posix.is_absolute()
        or PureWindowsPath(path).drive
        or PureWindowsPath(path).is_absolute()
        or "\\" in path
        or "#" in path
        or posix.as_posix() != path
        or any(part in ("", ".", "..") for part in path.split("/"))
    )


def _positive_id(name: str, value: object) -> None:
    if type(value) is not int or not 0 < value <= MAX_ID:
        raise ValueError(f"{name} debe ser un entero positivo de 32 bits, no {value!r}")


def _text(name: str, value: object) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} debe ser un texto no vacío")


def _finite(name: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise ValueError(f"{name} debe ser un número finito, no {value!r}")
    return float(value)


@dataclass(frozen=True, slots=True)
class Song:
    song_id: int
    title: str
    artist: str
    audio_path: str
    duration_seconds: float

    def __post_init__(self) -> None:
        _positive_id("song_id", self.song_id)
        _text("title", self.title)
        _text("artist", self.artist)
        if not is_relative_audio_path(self.audio_path):
            raise ValueError(f"audio_path debe ser relativo y canónico: {self.audio_path!r}")
        if PurePosixPath(self.audio_path).suffix.lower() not in AUDIO_EXTENSIONS:
            raise ValueError(f"audio_path no tiene una extensión de audio: {self.audio_path!r}")
        if self.audio_path.split("/")[0] == QUERIES_DIR:
            raise ValueError("audio_path no puede estar bajo la carpeta de candidatos")
        duration = _finite("duration_seconds", self.duration_seconds)
        if duration <= 0:
            raise ValueError("duration_seconds debe ser > 0")
        object.__setattr__(self, "duration_seconds", duration)


@dataclass(frozen=True, slots=True)
class AudioSegment:
    segment_id: int
    song_id: int
    segment_index: int
    start_seconds: float
    end_seconds: float
    audio_ref: str

    def __post_init__(self) -> None:
        _positive_id("segment_id", self.segment_id)
        _positive_id("song_id", self.song_id)
        if type(self.segment_index) is not int or self.segment_index < 0:
            raise ValueError(f"segment_index debe ser un entero >= 0, no {self.segment_index!r}")
        start = _finite("start_seconds", self.start_seconds)
        end = _finite("end_seconds", self.end_seconds)
        if start < 0:
            raise ValueError("start_seconds debe ser >= 0")
        if end <= start:
            raise ValueError("end_seconds debe ser > start_seconds")
        _text("audio_ref", self.audio_ref)
        object.__setattr__(self, "start_seconds", start)
        object.__setattr__(self, "end_seconds", end)


@dataclass(frozen=True, slots=True)
class SongEntry:
    """Canción con sus segmentos indexados, en orden."""

    song: Song
    segments: tuple[AudioSegment, ...]


@dataclass(frozen=True, slots=True)
class SegmentInterval:
    """Bloque de 5 s del candidato (el último puede ser más corto)."""

    index: int
    start_seconds: float
    end_seconds: float


@dataclass(frozen=True, slots=True)
class NeighborRow:
    """Fila que devuelve la búsqueda HNSW remota (sin metadatos de canción)."""

    segment_id: int
    song_id: int
    segment_index: int
    start_seconds: float
    end_seconds: float
    audio_ref: str
    score: float


@dataclass(frozen=True, slots=True)
class SegmentMatch:
    """Un segmento original vecino de un segmento del candidato."""

    segment_id: int
    song_id: int
    title: str
    artist: str
    source_segment_index: int
    source_start_seconds: float
    source_end_seconds: float
    audio_ref: str
    #: Similitud del motor (euclidean: 1 / (1 + distancia)). No es una probabilidad.
    score: float


@dataclass(frozen=True, slots=True)
class QuerySegmentResult:
    query_segment_index: int
    query_start_seconds: float
    query_end_seconds: float
    matches: tuple[SegmentMatch, ...]


@dataclass(frozen=True, slots=True)
class AudioSearchResult:
    audio_path: str
    k: int
    segments: tuple[QuerySegmentResult, ...]
