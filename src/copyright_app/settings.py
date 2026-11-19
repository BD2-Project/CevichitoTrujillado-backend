"""Configuración del backend leída de variables de entorno (solo parsing, stdlib).

=============================  ==============  ========================================
Variable                       Por defecto     Qué es
=============================  ==============  ========================================
``SOUPDB_HOST``                ``soupdb``      Host del servidor SoupDB (TCP)
``SOUPDB_PORT``                ``55432``       Puerto del protocolo SoupDB v2
``SOUPDB_TIMEOUT_SECONDS``     ``60``          Timeout de socket por petición
``SOUPDB_POOL_SIZE``           ``4``           Conexiones TCP simultáneas del backend
``COPYRIGHT_AUDIO_ROOT``       ``/audio``      Volumen de audio compartido con SoupDB
``COPYRIGHT_SEARCH_K``         ``5``           Vecinos HNSW por segmento (1–1000)
``COPYRIGHT_MAX_UPLOAD_MB``    ``50``          Tamaño máximo de un upload (MiB)
``COPYRIGHT_HOST``             ``127.0.0.1``   Interfaz HTTP del backend
``COPYRIGHT_PORT``             ``8000``        Puerto HTTP del backend
``COPYRIGHT_CORS_ORIGINS``     vacío           Orígenes del frontend, separados por comas
=============================  ==============  ========================================

Una variable ausente o vacía usa su valor por defecto; un valor presente pero
inválido es un error, nunca un default silencioso.
"""

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

_DIGITS = re.compile(r"[0-9]+")


class ConfigError(ValueError):
    """Una variable de entorno tiene un valor inválido."""


def parse_int(name: str, raw: str, *, minimum: int, maximum: int) -> int:
    value = raw.strip()
    if not _DIGITS.fullmatch(value):
        raise ConfigError(f"{name} debe ser un entero, no {raw!r}")
    number = int(value)
    if not minimum <= number <= maximum:
        raise ConfigError(f"{name} debe estar entre {minimum} y {maximum}, no {number}")
    return number


def parse_cors_origins(raw: str) -> tuple[str, ...]:
    origins: list[str] = []
    for item in raw.split(","):
        origin = item.strip().rstrip("/")
        if not origin:
            continue
        if origin == "*":
            raise ConfigError("COPYRIGHT_CORS_ORIGINS no admite '*'; enumera los orígenes")
        parts = urlsplit(origin)
        if (
            parts.scheme not in ("http", "https")
            or not parts.netloc
            or parts.path
            or parts.query
            or parts.fragment
        ):
            raise ConfigError(f"COPYRIGHT_CORS_ORIGINS: origen inválido {item.strip()!r}")
        if origin not in origins:
            origins.append(origin)
    return tuple(origins)


@dataclass(frozen=True, slots=True)
class Settings:
    soupdb_host: str = "soupdb"
    soupdb_port: int = 55432
    soupdb_timeout: float = 60.0
    pool_size: int = 4
    audio_root: Path = Path("/audio")
    search_k: int = 5
    max_upload_bytes: int = 50 * 1024 * 1024
    host: str = "127.0.0.1"
    port: int = 8000
    cors_origins: tuple[str, ...] = ()

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        source = os.environ if env is None else env

        def get(name: str) -> str | None:
            value = source.get(name)
            return None if value is None or not value.strip() else value.strip()

        def number(name: str, default: int, minimum: int, maximum: int) -> int:
            raw = get(name)
            return (
                default if raw is None else parse_int(name, raw, minimum=minimum, maximum=maximum)
            )

        audio_root = get("COPYRIGHT_AUDIO_ROOT")
        return cls(
            soupdb_host=get("SOUPDB_HOST") or "soupdb",
            soupdb_port=number("SOUPDB_PORT", 55432, 1, 65535),
            soupdb_timeout=float(number("SOUPDB_TIMEOUT_SECONDS", 60, 1, 3600)),
            pool_size=number("SOUPDB_POOL_SIZE", 4, 1, 64),
            audio_root=Path(audio_root).absolute() if audio_root else Path("/audio"),
            search_k=number("COPYRIGHT_SEARCH_K", 5, 1, 1000),
            max_upload_bytes=number("COPYRIGHT_MAX_UPLOAD_MB", 50, 1, 2048) * 1024 * 1024,
            host=get("COPYRIGHT_HOST") or "127.0.0.1",
            port=number("COPYRIGHT_PORT", 8000, 1, 65535),
            cors_origins=parse_cors_origins(source.get("COPYRIGHT_CORS_ORIGINS", "")),
        )
