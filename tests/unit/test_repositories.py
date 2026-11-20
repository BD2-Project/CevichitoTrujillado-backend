from contextlib import contextmanager

import pytest

from copyright_app.errors import IntegrityError, InvalidAudioError, ServiceUnavailableError
from copyright_app.infrastructure.repositories import (
    SoupDBCatalogRepository,
    SoupDBHistoryRepository,
    SoupDBSearchRepository,
)
from copyright_app.infrastructure.soupdb_client import (
    QueryResult,
    SoupDBConnectionError,
    SoupDBError,
)
from copyright_app.schemas import AnalysisResult
from tests.support.fakes import make_entry

REF = "queries/" + "a" * 32 + "/candidate.mp3#segment=000002"


class FakeClient:
    """Responde con funciones ``sql → QueryResult`` y registra todo."""

    def __init__(self, responder):
        self.responder = responder
        self.log: list[tuple[str, str, tuple]] = []
        self.in_transaction = False

    def query(self, sql):
        self.log.append(("query", sql, ()))
        return self.responder(sql, ())

    def run(self, sql, params=()):
        self.log.append(("run", sql, tuple(params)))
        return self.responder(sql, tuple(params))

    @contextmanager
    def transaction(self):
        self.log.append(("begin", "", ()))
        try:
            yield self
        except BaseException:
            self.log.append(("rollback", "", ()))
            raise
        self.log.append(("commit", "", ()))


class FakePool:
    def __init__(self, client):
        self.client = client

    @contextmanager
    def connection(self):
        yield self.client


def rows(*values):
    return QueryResult(rows=tuple(values), affected=len(values))


def test_search_sends_one_validated_literal_query():
    client = FakeClient(lambda sql, p: rows((7, 1, 2, 10.0, 15.0, "fma/a.mp3#segment=000002", 0.5)))

    found = SoupDBSearchRepository(FakePool(client)).similar_segments(REF, 5)

    ((kind, sql, params),) = client.log
    assert kind == "query" and params == ()
    assert f"SIMILAR_TO('{REF}', k=5) USING HNSW WITH METRIC=euclidean" in sql
    assert found[0].segment_id == 7 and found[0].score == 0.5


@pytest.mark.parametrize(
    "reference",
    [
        "fma/000/000002.mp3#segment=000000",  # un original, no un candidato
        "queries/" + "a" * 32 + "/candidate.mp3",  # sin segmento
        "queries/../x.mp3#segment=000000",
        "queries/" + "a" * 32 + "/candidate.mp3#segment=000000' OR '1'='1",
        "queries/" + "A" * 32 + "/candidate.mp3#segment=000000",
    ],
)
def test_search_rejects_references_that_are_not_server_generated(reference):
    client = FakeClient(lambda sql, p: rows())
    with pytest.raises(ValueError):
        SoupDBSearchRepository(FakePool(client)).similar_segments(reference, 5)
    assert client.log == []


@pytest.mark.parametrize("k", [0, 1001, True, 2.5])
def test_search_k_bounds(k):
    with pytest.raises(ValueError):
        SoupDBSearchRepository(FakePool(FakeClient(lambda s, p: rows()))).similar_segments(REF, k)


def test_search_maps_server_errors():
    def vectorize_fails(sql, p):
        raise SoupDBError(2, "failed to vectorize query for profile 'x': boom")

    def no_index(sql, p):
        raise SoupDBError(2, "unknown table 'audio_segments'")

    with pytest.raises(InvalidAudioError):
        SoupDBSearchRepository(FakePool(FakeClient(vectorize_fails))).similar_segments(REF, 5)
    with pytest.raises(ServiceUnavailableError):
        SoupDBSearchRepository(FakePool(FakeClient(no_index))).similar_segments(REF, 5)


def test_connection_failures_become_service_unavailable():
    class BrokenPool:
        @contextmanager
        def connection(self):
            raise SoupDBConnectionError("down")
            yield

    with pytest.raises(ServiceUnavailableError):
        SoupDBCatalogRepository(BrokenPool()).list_songs()


def test_index_ready_reads_sys_plugin_indexes():
    good = '{"dimension": 128, "metric": "euclidean", "profile_id": "copyright-audio-v1"}'
    bad = '{"dimension": 64, "metric": "euclidean", "profile_id": "copyright-audio-v1"}'

    def responder(options):
        return lambda sql, p: rows(("audio_segments", "audio_ref", "HNSW", options))

    assert SoupDBSearchRepository(FakePool(FakeClient(responder(good)))).index_ready()
    assert not SoupDBSearchRepository(FakePool(FakeClient(responder(bad)))).index_ready()
    assert not SoupDBSearchRepository(FakePool(FakeClient(lambda s, p: rows()))).index_ready()


def test_register_song_uses_parameters_inside_a_transaction():
    entry = make_entry(1, "Tit'le", "fma/000/000001.mp3")
    client = FakeClient(lambda sql, p: rows())

    assert SoupDBCatalogRepository(FakePool(client)).register_song(entry.song, entry.segments)

    kinds = [kind for kind, _sql, _p in client.log]
    assert kinds[0] == "run" and "begin" in kinds and kinds[-1] == "commit"
    inserts = [(sql, p) for kind, sql, p in client.log if sql.startswith("INSERT")]
    assert inserts[0] == (
        "INSERT INTO songs VALUES ($1, $2, $3, $4, $5)",
        (1, "Tit'le", "Artista", "fma/000/000001.mp3", 15.0),
    )
    assert len(inserts) == 4
    assert all("'" not in sql for sql, _ in inserts)  # nada interpolado


def test_register_song_validates_segments_before_any_sql():
    entry = make_entry(1, "T", "fma/000/000001.mp3")
    client = FakeClient(lambda sql, p: rows())
    repo = SoupDBCatalogRepository(FakePool(client))

    with pytest.raises(IntegrityError, match="consecutivos"):
        repo.register_song(entry.song, entry.segments[1:])
    with pytest.raises(IntegrityError, match="no tiene segmentos"):
        repo.register_song(entry.song, ())
    assert client.log == []


def test_history_save_inserts_header_last_in_a_transaction():
    client = FakeClient(lambda sql, p: rows())
    history = SoupDBHistoryRepository(FakePool(client), clock=lambda: "2026-01-01T00:00:00.000000Z")
    result = AnalysisResult(analysis_id="d" * 32, filename="x'.mp3", segment_seconds=5.0)

    stored = history.save(result)

    inserts = [sql for _k, sql, _p in client.log if sql.startswith("INSERT")]
    assert inserts == ["INSERT INTO analyses VALUES ($1, $2, $3, $4, $5)"]
    assert client.log[0][0] == "begin" and client.log[-1][0] == "commit"
    assert stored.created_at == "2026-01-01T00:00:00.000000Z"
    with pytest.raises(IntegrityError):
        history.save(result.model_copy(update={"analysis_id": "../x"}))
