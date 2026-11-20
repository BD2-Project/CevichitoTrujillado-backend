"""Backend y SoupDB comparten el volumen de audio; el backend escribe candidatos."""

import logging
import re
import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO

from copyright_app.config import AUDIO_EXTENSIONS, QUERIES_DIR
from copyright_app.domain.models import is_relative_audio_path
from copyright_app.errors import InvalidAudioError

logger = logging.getLogger(__name__)

_CHUNK_BYTES = 1024 * 1024
_ANALYSIS_ID = re.compile(r"[0-9a-f]{32}")


class SharedAudioStore:
    """Acceso acotado al volumen ``/audio``."""

    def __init__(self, root: str | Path, *, max_upload_bytes: int | None = None) -> None:
        self._root = Path(root).absolute()
        self._max_upload_bytes = max_upload_bytes

    @property
    def root(self) -> Path:
        return self._root

    @contextmanager
    def stage(
        self, analysis_id: str, extension: str, upload: BinaryIO
    ) -> Iterator[tuple[str, Path]]:
        """Escribe ``upload`` en ``queries/<analysis_id>/candidate<ext>`` y lo borra al salir."""
        if not _ANALYSIS_ID.fullmatch(analysis_id):
            raise ValueError(f"analysis_id inválido: {analysis_id!r}")
        if extension and extension not in AUDIO_EXTENSIONS:
            raise ValueError(f"extensión no admitida: {extension!r}")
        relative = f"{QUERIES_DIR}/{analysis_id}/candidate{extension}"
        target = self._root / relative
        try:
            target.parent.mkdir(parents=True, exist_ok=False)
            written = 0
            with target.open("xb") as out:
                while chunk := upload.read(_CHUNK_BYTES):
                    written += len(chunk)
                    if self._max_upload_bytes is not None and written > self._max_upload_bytes:
                        raise InvalidAudioError(
                            f"el archivo supera el máximo de {self._max_upload_bytes} bytes"
                        )
                    out.write(chunk)
            if written == 0:
                raise InvalidAudioError("el archivo subido está vacío")
            yield relative, target
        finally:
            target.unlink(missing_ok=True)
            try:
                target.parent.rmdir()
            except FileNotFoundError:
                pass
            except OSError as exc:
                logger.warning("no se pudo borrar el staging %s: %s", target.parent.name, exc)

    def reference_file(self, audio_path: str) -> Path | None:
        """Devuelve un original accesible dentro del volumen, fuera de queries/."""
        if not is_relative_audio_path(audio_path) or audio_path.split("/")[0] == QUERIES_DIR:
            return None
        if Path(audio_path).suffix.lower() not in AUDIO_EXTENSIONS:
            return None
        root = self._root.resolve()
        path = (root / audio_path).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            return None
        return path

    def clear_stale_queries(self) -> int:
        """Borra carpetas de candidatos huérfanas (p. ej. tras un apagado brusco)."""
        queries = self._root / QUERIES_DIR
        removed = 0
        if not queries.is_dir():
            return 0
        for entry in queries.iterdir():
            if entry.is_dir() and _ANALYSIS_ID.fullmatch(entry.name):
                shutil.rmtree(entry, ignore_errors=True)
                removed += 1
        return removed
