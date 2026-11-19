"""Catálogo de solo lectura para el frontend y audio original por ``song_id``.

El archivo se resuelve **solo** a partir de un ``song_id``: primero se lee su
``audio_path`` de SoupDB (TCP) y después se resuelve bajo ``/audio`` con
:meth:`AudioStore.reference_file`. El cliente nunca envía rutas.
"""

import logging
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from copyright_app.config import AUDIO_MEDIA_TYPES
from copyright_app.domain.models import SongEntry
from copyright_app.domain.ports import AudioStore, CatalogRepository

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ReferenceAudio:
    path: Path
    media_type: str
    #: Para ``Content-Disposition``: solo el último componente de ``audio_path``.
    filename: str


class CatalogLibrary:
    def __init__(self, catalog: CatalogRepository, store: AudioStore) -> None:
        self._catalog = catalog
        self._store = store

    def list_songs(self) -> list[SongEntry]:
        return self._catalog.list_songs()

    def get_song(self, song_id: int) -> SongEntry | None:
        return self._catalog.get_song(song_id)

    def reference_audio(self, song_id: int) -> ReferenceAudio | None:
        entry = self._catalog.get_song(song_id)
        if entry is None:
            return None
        audio_path = entry.song.audio_path
        media_type = AUDIO_MEDIA_TYPES.get(PurePosixPath(audio_path).suffix.lower())
        path = self._store.reference_file(audio_path) if media_type else None
        if path is None or media_type is None:
            logger.warning("no se sirve el audio de song_id=%d", song_id)
            return None
        return ReferenceAudio(path, media_type, PurePosixPath(audio_path).name)
