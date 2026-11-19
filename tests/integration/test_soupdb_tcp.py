"""Cliente y repositorios contra un servidor SoupDB real (proceso externo, solo TCP)."""

from concurrent.futures import ThreadPoolExecutor

import pytest

from copyright_app.config import ANALYSES, ANALYSIS_MATCHES, AUDIO_SEGMENTS, SONGS
from copyright_app.domain.models import AudioSegment, Song, segment_audio_ref
from copyright_app.errors import IncompatibleSchemaError, IntegrityError
from copyright_app.infrastructure.repositories import (
    CATALOG_TABLES,
    HISTORY_TABLES,
    SoupDBCatalogRepository,
    SoupDBHistoryRepository,
    SoupDBSchema,
)
from copyright_app.infrastructure.soupdb_client import (
    Column,
    SoupDBClient,
    SoupDBError,
    SoupDBPool,
)
from copyright_app.schemas import AnalysisResult, CopyrightMatch

pytestmark = pytest.mark.integration


@pytest.fixture
def client(soupdb_server):
    with SoupDBClient(soupdb_server.host, soupdb_server.port, timeout=30) as c:
        yield c


@pytest.fixture
def pool(soupdb_server):
    p = SoupDBPool(lambda: SoupDBClient(soupdb_server.host, soupdb_server.port, timeout=30), 3)
    yield p
    p.close()


def song(song_id=1, path="fma/000/000001.mp3", title="Canción", artist="O'Neil"):
    return Song(song_id, title, artist, path, 12.5)


def segments(s: Song, first=1):
    bounds = [(0, 0.0, 5.0), (1, 5.0, 10.0), (2, 10.0, 12.5)]
    return tuple(
        AudioSegment(first + i, s.song_id, i, a, b, segment_audio_ref(s.audio_path, i))
        for i, a, b in bounds
    )


# --- protocolo ------------------------------------------------------------------------


def test_ping(client):
    client.ping()


def test_create_insert_select_with_v2_column_header(client):
    client.query("CREATE TABLE t (id INT, name TEXT, score FLOAT, ok BOOL)")
    assert client.query("INSERT INTO t VALUES (1, 'a', 0.5, TRUE)").affected == 1

    result = client.query("SELECT * FROM t")

    assert result.columns[0] == Column("id", "INT", 0, 4, 0)
    assert [c.type_name for c in result.columns] == ["INT", "TEXT", "FLOAT", "BOOL"]
    assert result.rows == ((1, "a", 0.5, True),)


def test_prepared_statements_bind_values_without_interpolation(client):
    client.query("CREATE TABLE t (id INT, name TEXT, score FLOAT, ok BOOL)")
    hostile = "x'); DROP TABLE t; --"
    client.prepare("ins", "INSERT INTO t VALUES ($1, $2, $3, $4)")

    assert client.execute("ins", (1, hostile, 0.25, False)).affected == 1
    assert client.execute("ins", (2, "ñandú ☃", -1.5, True)).affected == 1
    client.prepare("by_name", "SELECT id FROM t WHERE name = $1")
    assert client.execute("by_name", (hostile,)).rows == ((1,),)
    client.deallocate("by_name")
    with pytest.raises(SoupDBError):
        client.execute("by_name", (hostile,))
    assert len(client.query("SELECT * FROM t").rows) == 2  # la tabla sigue ahí


def test_run_caches_prepared_statements(client):
    client.query("CREATE TABLE t (id INT)")
    for i in range(3):
        client.run("INSERT INTO t VALUES ($1)", (i,))

    assert client.run("SELECT id FROM t WHERE id = $1", (2,)).rows == ((2,),)
    assert len(client._statements) == 2


def test_transactions_commit_and_rollback(client):
    client.query("CREATE TABLE t (id INT)")
    with client.transaction():
        client.run("INSERT INTO t VALUES ($1)", (1,))
    client.begin()
    client.run("INSERT INTO t VALUES ($1)", (2,))
    client.rollback()
    with pytest.raises(RuntimeError), client.transaction():
        client.run("INSERT INTO t VALUES ($1)", (3,))
        raise RuntimeError("falla a mitad")

    assert client.query("SELECT id FROM t").rows == ((1,),)


def test_server_errors_keep_the_connection_usable(client):
    with pytest.raises(SoupDBError) as info:
        client.query("SELECT * FROM no_existe")
    assert info.value.code_name == "EXECUTION"
    with pytest.raises(SoupDBError) as info:
        client.query("SELEC mal")
    assert info.value.code_name == "PARSE"
    client.ping()


def test_system_tables_are_plain_sql(client):
    client.query("CREATE TABLE t (id INT, name TEXT)")

    tables = {row[0] for row in client.query("SELECT table_name FROM SysTables").rows}
    columns = client.run(
        "SELECT position, column_name, type_name FROM SysColumns WHERE table_name = $1", ("t",)
    ).rows

    assert "t" in tables
    assert sorted(columns) == [(0, "id", "INT"), (1, "name", "TEXT")]


# --- repositorios ----------------------------------------------------------------------


def test_schema_is_idempotent_and_detects_incompatible_tables(pool, client):
    schema = SoupDBSchema(pool)
    schema.ensure(CATALOG_TABLES + HISTORY_TABLES)
    schema.ensure(CATALOG_TABLES + HISTORY_TABLES)

    names = {row[0] for row in client.query("SELECT table_name FROM SysTables").rows}
    assert {SONGS, AUDIO_SEGMENTS, ANALYSES, ANALYSIS_MATCHES} <= names
    indexes = client.query("SELECT table_name, column_name FROM SysIndexes").rows
    assert (AUDIO_SEGMENTS, "song_id") in indexes
    assert len([r for r in indexes if r == (SONGS, "song_id")]) == 1


def test_schema_rejects_an_existing_table_with_another_schema(pool, client):
    client.query("CREATE TABLE songs (song_id INT, title TEXT)")

    with pytest.raises(IncompatibleSchemaError, match="songs"):
        SoupDBSchema(pool).ensure(CATALOG_TABLES)


def test_catalog_repository_round_trip_and_invariants(pool):
    SoupDBSchema(pool).ensure(CATALOG_TABLES)
    repo = SoupDBCatalogRepository(pool)
    first = song()
    second = song(2, "fma/000/000002.mp3", "Otra", "B")

    assert repo.register_song(first, segments(first)) is True
    assert repo.register_song(first, segments(first)) is False  # idempotente
    assert repo.register_song(second, segments(second, first=11)) is True

    assert repo.counts() == (2, 6)
    entry = repo.get_song(1)
    assert entry.song == first and entry.segments == segments(first)
    assert [e.song.song_id for e in repo.list_songs()] == [1, 2]
    assert repo.songs_by_id([2, 1, 2, 99]) == {1: first, 2: second}
    assert repo.get_song(99) is None

    changed = Song(1, "Cambiada", "O'Neil", first.audio_path, 12.5)
    with pytest.raises(IntegrityError, match="otros datos"):
        repo.register_song(changed, segments(changed))
    clash = song(3, first.audio_path)
    with pytest.raises(IntegrityError, match="audio_path"):
        repo.register_song(clash, segments(clash, first=21))
    reused = song(4, "fma/000/000004.mp3")
    with pytest.raises(IntegrityError, match="segment_id"):
        repo.register_song(reused, segments(reused, first=2))
    assert repo.counts() == (2, 6)  # los fallos no dejaron filas (transacción)


def stored_result(analysis_id: str, filename: str = "remix.wav") -> AnalysisResult:
    match = CopyrightMatch(
        query_start=0.0,
        query_end=10.0,
        song_id=1,
        title="Canción",
        artist="O'Neil",
        source_start=5.0,
        source_end=15.0,
        score=0.9,
        segment_count=2,
        min_score=0.85,
        max_score=0.95,
        alignment_offset_seconds=5.0,
        is_best_run=True,
    )
    return AnalysisResult(
        analysis_id=analysis_id, filename=filename, segment_seconds=5.0, matches=[match]
    )


def test_history_repository_round_trip(pool):
    schema = SoupDBSchema(pool)
    schema.ensure(CATALOG_TABLES + HISTORY_TABLES)
    SoupDBCatalogRepository(pool).register_song(song(), segments(song()))
    stamps = iter(["2026-11-18T10:00:00.000000Z", "2026-11-18T11:00:00.000000Z", "x"])
    history = SoupDBHistoryRepository(pool, clock=lambda: next(stamps))
    first, second = "a" * 32, "b" * 32

    saved = history.save(stored_result(first, "x'); DROP TABLE analyses; --.wav"))
    history.save(AnalysisResult(analysis_id=second, filename="vacío.mp3", segment_seconds=5.0))

    assert saved.created_at == "2026-11-18T10:00:00.000000Z"
    assert history.get(first) == saved
    summaries = history.list_summaries()
    assert [s.analysis_id for s in summaries] == [second, first]
    assert (summaries[1].match_count, summaries[1].song_count, summaries[1].best_score) == (
        1,
        1,
        0.9,
    )
    assert summaries[0].best_score is None
    assert history.get("c" * 32) is None
    assert history.get("../etc") is None
    with pytest.raises(IntegrityError, match="ya existe"):
        history.save(stored_result(first))


def test_pool_serves_concurrent_requests(pool):
    SoupDBSchema(pool).ensure(CATALOG_TABLES)
    repo = SoupDBCatalogRepository(pool)
    for i in range(1, 6):
        s = song(i, f"fma/000/00000{i}.mp3")
        repo.register_song(s, segments(s, first=i * 10))

    with ThreadPoolExecutor(max_workers=6) as executor:
        results = list(executor.map(lambda i: repo.get_song(i % 5 + 1), range(30)))

    assert all(entry is not None for entry in results)
    assert repo.counts() == (5, 15)
