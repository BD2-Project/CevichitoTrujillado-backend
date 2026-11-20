"""Prepara el catálogo y el índice HNSW a través de SoupDB."""

from __future__ import annotations

import argparse
import json
import logging
import math
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from copyright_app.config import (
    AUDIO_SEGMENTS,
    CODEBOOK_K,
    INDEX_KIND,
    INDEX_NAME,
    INDEXED_COLUMN,
    METRIC,
    PROFILE_ID,
    SEGMENT_SECONDS,
    VECTOR_DIMENSION,
)
from copyright_app.domain.models import AudioSegment, Song, segment_audio_ref
from copyright_app.errors import AppError, IntegrityError
from copyright_app.infrastructure.repositories import (
    CATALOG_TABLES,
    SoupDBCatalogRepository,
    SoupDBSchema,
    SoupDBSearchRepository,
    text_literal,
)
from copyright_app.infrastructure.soupdb_client import (
    SoupDBClient,
    SoupDBClientError,
    SoupDBPool,
)
from copyright_app.settings import Settings

logger = logging.getLogger("copyright_app.bootstrap")

CATALOG_VERSION = 1


class BootstrapError(AppError):
    """El bootstrap no puede continuar; el mensaje indica la causa."""


@dataclass(frozen=True, slots=True)
class CatalogEntry:
    song: Song
    segments: tuple[AudioSegment, ...]


def load_catalog(path: str | Path) -> list[CatalogEntry]:
    """Lee y valida el catálogo JSON (estructura, ids, referencias canónicas)."""
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BootstrapError(f"no se pudo leer el catálogo {path}: {exc}") from exc
    if not isinstance(doc, dict) or doc.get("version") != CATALOG_VERSION:
        raise BootstrapError(f"catálogo con versión no soportada: {path}")
    if doc.get("segment_seconds") != SEGMENT_SECONDS:
        raise BootstrapError(f"el catálogo debe usar segmentos de {SEGMENT_SECONDS} s")
    entries: list[CatalogEntry] = []
    seen_songs: set[int] = set()
    seen_segments: set[int] = set()
    seen_paths: set[str] = set()
    try:
        for raw in doc["songs"]:
            song = Song(
                raw["song_id"],
                raw["title"],
                raw["artist"],
                raw["audio_path"],
                raw["duration_seconds"],
            )
            segments = tuple(
                AudioSegment(
                    raw["first_segment_id"] + position,
                    song.song_id,
                    index,
                    start,
                    end,
                    segment_audio_ref(song.audio_path, index),
                )
                for position, (index, start, end) in enumerate(raw["segments"])
            )
            if song.song_id in seen_songs or song.audio_path in seen_paths:
                raise BootstrapError(f"canción repetida en el catálogo: {song.song_id}")
            for segment in segments:
                if segment.segment_id in seen_segments:
                    raise BootstrapError(f"segment_id repetido: {segment.segment_id}")
                seen_segments.add(segment.segment_id)
            seen_songs.add(song.song_id)
            seen_paths.add(song.audio_path)
            SoupDBCatalogRepository._validate_segments(song, segments)
            entries.append(CatalogEntry(song, segments))
    except (KeyError, TypeError, ValueError, IntegrityError) as exc:
        raise BootstrapError(f"catálogo inválido: {exc}") from exc
    if doc.get("song_count") != len(entries) or doc.get("segment_count") != len(seen_segments):
        raise BootstrapError("song_count/segment_count no coinciden con el contenido")
    return entries


def write_training_manifest(entries: Sequence[CatalogEntry], output: Path) -> int:
    """Manifest ``audio_segment`` (formato de ``soupdb_vector.multimedia.manifest``)."""
    rows = sorted(
        (
            {
                "id": s.segment_id,
                "type": "audio_segment",
                "path": entry.song.audio_path,
                "segment": s.segment_index,
                "start": round(s.start_seconds, 3),
                "end": round(s.end_seconds, 3),
            }
            for entry in entries
            for s in entry.segments
        ),
        key=lambda row: row["id"],
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    partial = output.with_name(output.name + ".partial")
    with partial.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    partial.replace(output)
    return len(rows)


def verify_audio(entries: Sequence[CatalogEntry], audio_root: Path, *, decode: bool) -> None:
    """Cada original existe bajo ``audio_root``; con ``decode``, su segmentación coincide."""
    from copyright_app.infrastructure.audio_store import SharedAudioStore

    store = SharedAudioStore(audio_root)
    segmenter = None
    if decode:
        from copyright_app.infrastructure.segmentation import LibrosaSegmenter

        segmenter = LibrosaSegmenter()
    for entry in entries:
        path = store.reference_file(entry.song.audio_path)
        if path is None:
            raise BootstrapError(f"falta el audio {entry.song.audio_path!r} bajo {audio_root}")
        if segmenter is not None:
            intervals = segmenter.intervals(path)
            expected = [(s.segment_index, s.start_seconds, s.end_seconds) for s in entry.segments]
            actual = [(i.index, i.start_seconds, i.end_seconds) for i in intervals]
            if len(actual) != len(expected) or any(
                a[0] != e[0] or not math.isclose(a[2], e[2], abs_tol=1e-6)
                for a, e in zip(actual, expected, strict=False)
            ):
                raise BootstrapError(
                    f"la segmentación de {entry.song.audio_path!r} no coincide con el catálogo"
                )


def extension_sql(storage_root: str, profile_root: str, codebook: str, cache: str) -> str:
    profiles = {
        PROFILE_ID: {
            "kind": "audio_segment",
            "root": profile_root,
            "codebook": codebook,
            "cache": cache,
        }
    }
    return (
        f"CREATE EXTENSION {INDEX_KIND} WITH (storage_root = {text_literal(storage_root)}, "
        f"profiles = {text_literal(json.dumps(profiles, sort_keys=True))})"
    )


def index_sql() -> str:
    return (
        f"CREATE INDEX {INDEX_NAME} ON {AUDIO_SEGMENTS} ({INDEXED_COLUMN}) USING {INDEX_KIND} "
        f"WITH METRIC={METRIC}, DIMENSION={VECTOR_DIMENSION}, PROFILE={text_literal(PROFILE_ID)}"
    )


def ensure_extension(
    client: SoupDBClient, storage_root: str, profile_root: str, codebook: str, cache: str
) -> bool:
    """``CREATE EXTENSION HNSW`` si no está en ``SysPlugins``; si está, la valida."""
    rows = client.run(
        "SELECT enabled, config_json FROM SysPlugins WHERE plugin_name = $1", (INDEX_KIND,)
    ).rows
    if rows:
        enabled, config_json = rows[0]
        config = json.loads(config_json or "{}")
        profile = config.get("profiles", {}).get(PROFILE_ID)
        expected = {"kind": "audio_segment", "root": profile_root, "codebook": codebook}
        if not enabled or not profile or any(profile.get(k) != v for k, v in expected.items()):
            raise BootstrapError(
                f"la extensión {INDEX_KIND} ya está configurada con otro perfil: {profile}"
            )
        return False
    client.query(extension_sql(storage_root, profile_root, codebook, cache))
    return True


def bootstrap_catalog(
    settings: Settings,
    entries: Sequence[CatalogEntry],
    *,
    storage_root: str,
    profile_root: str,
    codebook: str,
    cache: str,
) -> dict[str, int | bool]:
    pool = SoupDBPool(
        lambda: SoupDBClient(settings.soupdb_host, settings.soupdb_port, timeout=3600), 1
    )
    try:
        SoupDBSchema(pool).ensure(CATALOG_TABLES)
        catalog = SoupDBCatalogRepository(pool)
        search = SoupDBSearchRepository(pool)
        index_exists = search.index_ready()
        inserted = 0
        for entry in entries:
            if index_exists:
                # Con el HNSW creado solo se verifica: la canción debe existir idéntica.
                stored = catalog.get_song(entry.song.song_id)
                if stored is None or stored.song != entry.song or stored.segments != entry.segments:
                    raise BootstrapError(
                        f"el índice ya existe y la canción {entry.song.song_id} no coincide"
                    )
                continue
            inserted += int(catalog.register_song(entry.song, entry.segments))
        songs, segments = catalog.counts()
        expected_segments = sum(len(e.segments) for e in entries)
        if (songs, segments) != (len(entries), expected_segments):
            raise BootstrapError(
                f"la base tiene {songs} canciones y {segments} segmentos; se esperaban "
                f"{len(entries)} y {expected_segments}"
            )
        with pool.connection() as client:
            extension_created = ensure_extension(
                client, storage_root, profile_root, codebook, cache
            )
            index_created = False
            if not index_exists:
                if client.run(
                    "SELECT index_name FROM SysPluginIndexes WHERE index_name = $1", (INDEX_NAME,)
                ).rows:
                    raise BootstrapError(f"{INDEX_NAME} existe con otra configuración")
                client.query(index_sql())
                index_created = True
            count_rows = client.run(
                "SELECT item_count FROM SysPluginIndexes WHERE index_name = $1", (INDEX_NAME,)
            ).rows
        if not search.index_ready():
            raise BootstrapError(f"{INDEX_NAME} no quedó configurado como se esperaba")
        item_count = count_rows[0][0] if count_rows else None
        if item_count is not None and item_count != expected_segments:
            raise BootstrapError(
                f"el HNSW tiene {item_count} vectores; se esperaban {expected_segments}"
            )
        return {
            "songs": songs,
            "segments": segments,
            "inserted_songs": inserted,
            "extension_created": extension_created,
            "index_created": index_created,
        }
    finally:
        pool.close()


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(prog="copyright-bootstrap", description=__doc__.split("\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)

    manifest = commands.add_parser("training-manifest", help="catálogo JSON → manifest JSONL")
    manifest.add_argument("--catalog", type=Path, required=True)
    manifest.add_argument("--audio-root", type=Path, required=True)
    manifest.add_argument("--output", type=Path, required=True)
    manifest.add_argument(
        "--verify-audio",
        action="store_true",
        help="decodifica cada original y comprueba su segmentación (lento)",
    )

    catalog = commands.add_parser("catalog", help="inserta el catálogo y crea el HNSW por TCP")
    catalog.add_argument("--catalog", type=Path, required=True)
    catalog.add_argument("--storage-root", required=True, help="ruta del HNSW en SoupDB")
    catalog.add_argument("--profile-root", required=True, help="raíz /audio en SoupDB")
    catalog.add_argument("--codebook", required=True, help="codebook.npz en SoupDB")
    catalog.add_argument("--cache", required=True, help="caché de MFCC en SoupDB")

    args = parser.parse_args(argv)
    try:
        entries = load_catalog(args.catalog)
        if args.command == "training-manifest":
            verify_audio(entries, args.audio_root, decode=args.verify_audio)
            count = write_training_manifest(entries, args.output)
            print(f"manifest: {count} segmentos de {len(entries)} canciones -> {args.output}")
            print(f"siguiente: codebook K={CODEBOOK_K} con el plugin soupdb-vector en SoupDB")
        else:
            summary = bootstrap_catalog(
                Settings.from_env(),
                entries,
                storage_root=args.storage_root,
                profile_root=args.profile_root,
                codebook=args.codebook,
                cache=args.cache,
            )
            print(json.dumps(summary, sort_keys=True))
    except (AppError, SoupDBClientError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
