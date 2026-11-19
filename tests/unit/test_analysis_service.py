"""Servicio de análisis con dobles de los puertos: sin SoupDB ni audio real."""

import io
import re

import pytest

from copyright_app.errors import InvalidAudioError, RepositoryError
from copyright_app.infrastructure.audio_store import SharedAudioStore
from copyright_app.services.analysis import CopyrightAnalyzer, display_filename
from tests.support.fakes import (
    FakeSearch,
    FakeSegmenter,
    InMemoryCatalog,
    InMemoryHistory,
    make_entry,
    neighbor,
)

ALPHA = make_entry(1, "Alpha", "fma/000/000001.mp3", segments=3, first=1)
BETA = make_entry(2, "Beta", "fma/000/000002.mp3", segments=3, first=11)


def analyzer(tmp_path, neighbors, *, segments=3, history=None, k=5):
    search = FakeSearch(neighbors)
    search.root = tmp_path
    service = CopyrightAnalyzer(
        store=SharedAudioStore(tmp_path),
        segmenter=FakeSegmenter(segments, tail=3.0),
        search=search,
        catalog=InMemoryCatalog([ALPHA, BETA]),
        history=history,
        search_k=k,
        new_id=lambda: "f" * 32,
    )
    return service, search


def test_runs_follow_the_remote_neighbors(tmp_path):
    neighbors = {
        0: [neighbor(ALPHA, 0, 0.99), neighbor(BETA, 2, 0.4)],
        1: [neighbor(ALPHA, 1, 0.98)],
        2: [neighbor(ALPHA, 2, 0.97)],
    }
    history = InMemoryHistory()
    service, search = analyzer(tmp_path, neighbors, history=history, k=7)

    result = service.analyze(io.BytesIO(b"audio"), "../../mi remix.mp3")

    assert result.analysis_id == "f" * 32
    assert result.filename == "mi remix.mp3"
    top = result.matches[0]
    assert (top.title, top.segment_count, top.is_best_run) == ("Alpha", 3, True)
    assert (top.query_start, top.query_end, top.source_end) == (0.0, 13.0, 15.0)
    assert result.matches[-1].title == "Beta"
    # Cada segmento se consulta con la referencia del volumen compartido mientras existe.
    refs = [ref for ref, _k, _exists in search.calls]
    assert refs == [f"queries/{'f' * 32}/candidate.mp3#segment={i:06d}" for i in range(3)]
    assert all(k == 7 and exists for _ref, k, exists in search.calls)
    assert not any((tmp_path / "queries").iterdir())  # candidato borrado
    assert history.get("f" * 32).created_at  # guardado en el historial


def test_without_history_returns_the_plain_result(tmp_path):
    service, _ = analyzer(tmp_path, {})

    result = service.analyze(io.BytesIO(b"audio"), "x.wav")

    assert result.matches == [] and not hasattr(result, "created_at")


def test_unknown_extension_is_staged_without_extension(tmp_path):
    service, search = analyzer(tmp_path, {}, segments=1)

    service.analyze(io.BytesIO(b"audio"), "evil.exe")

    assert re.fullmatch(r"queries/f{32}/candidate#segment=000000", search.calls[0][0])


def test_no_segments_is_invalid_audio_and_cleans_up(tmp_path):
    service, search = analyzer(tmp_path, {}, segments=0)

    with pytest.raises(InvalidAudioError, match="2.5 s"):
        service.analyze(io.BytesIO(b"audio"), "x.wav")
    assert search.calls == []
    assert not any((tmp_path / "queries").iterdir())


def test_neighbor_of_an_unknown_song_is_an_inconsistency(tmp_path):
    ghost = make_entry(99, "Ghost", "fma/x.mp3", segments=1, first=900)
    service, _ = analyzer(tmp_path, {0: [neighbor(ghost, 0, 0.5)]}, segments=1)

    with pytest.raises(RepositoryError, match="song_id=99"):
        service.analyze(io.BytesIO(b"audio"), "x.wav")


@pytest.mark.parametrize("k", [0, -1, True, 2.0])
def test_search_k_is_validated(tmp_path, k):
    with pytest.raises(ValueError):
        analyzer(tmp_path, {}, k=k)


@pytest.mark.parametrize(
    ("raw", "shown"),
    [
        ("../../evil.wav", "evil.wav"),
        ("..\\..\\evil.wav", "evil.wav"),
        ("C:\\x\\evil.wav", "evil.wav"),
        ("", "upload"),
        (None, "upload"),
        ("..", "upload"),
        ("a\x00b.mp3", "ab.mp3"),
    ],
)
def test_display_filename(raw, shown):
    assert display_filename(raw) == shown
