import io
import json
from pathlib import Path

import pytest

from copyright_app import bootstrap
from copyright_app.domain.models import Song, is_relative_audio_path
from copyright_app.errors import InvalidAudioError
from copyright_app.infrastructure.audio_store import SharedAudioStore
from copyright_app.infrastructure.segmentation import LibrosaSegmenter, intervals_for_length
from copyright_app.settings import ConfigError, Settings, parse_cors_origins
from tests.support.audio import write_wav

CATALOG = Path(__file__).resolve().parents[2] / "data" / "fma_small_catalog.json"
ID = "e" * 32


def test_stage_writes_under_queries_and_always_cleans_up(tmp_path):
    store = SharedAudioStore(tmp_path)
    with store.stage(ID, ".mp3", io.BytesIO(b"abc")) as (reference, path):
        assert reference == f"queries/{ID}/candidate.mp3"
        assert path.read_bytes() == b"abc"
    assert not path.exists() and not path.parent.exists()
    with pytest.raises(RuntimeError), store.stage(ID, ".wav", io.BytesIO(b"x")) as (_, path):
        raise RuntimeError
    assert not path.parent.exists()


@pytest.mark.parametrize(("data", "limit"), [(b"", None), (b"x" * 11, 10)])
def test_stage_rejects_empty_or_oversized_uploads(tmp_path, data, limit):
    store = SharedAudioStore(tmp_path, max_upload_bytes=limit)
    with pytest.raises(InvalidAudioError), store.stage(ID, ".wav", io.BytesIO(data)):
        pass
    assert not any((tmp_path / "queries").iterdir())


@pytest.mark.parametrize(("analysis_id", "ext"), [("../x", ".wav"), (ID, ".exe"), ("A" * 32, "")])
def test_stage_never_uses_client_controlled_paths(tmp_path, analysis_id, ext):
    with (
        pytest.raises(ValueError),
        SharedAudioStore(tmp_path).stage(analysis_id, ext, io.BytesIO(b"x")),
    ):
        pass


def test_clear_stale_queries_only_removes_analysis_folders(tmp_path):
    (tmp_path / "queries" / ID).mkdir(parents=True)
    (tmp_path / "queries" / "keep").mkdir()

    assert SharedAudioStore(tmp_path).clear_stale_queries() == 1
    assert [p.name for p in (tmp_path / "queries").iterdir()] == ["keep"]


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (13.0, [(0, 0.0, 5.0), (1, 5.0, 10.0), (2, 10.0, 13.0)]),
        (12.0, [(0, 0.0, 5.0), (1, 5.0, 10.0)]),  # cola de 2 s descartada
        (12.5, [(0, 0.0, 5.0), (1, 5.0, 10.0), (2, 10.0, 12.5)]),  # 2.5 s se conserva
        (2.0, []),
        (0.0, []),
    ],
)
def test_intervals_match_the_profile_rule(seconds, expected):
    got = intervals_for_length(round(seconds * 22050))
    assert [(i.index, i.start_seconds, i.end_seconds) for i in got] == expected


def test_librosa_segmenter_decodes_real_audio(tmp_path):
    path = write_wav(tmp_path / "a.wav", 13.0, 300.0, 1)
    assert len(LibrosaSegmenter().intervals(path)) == 3
    (tmp_path / "bad.wav").write_bytes(b"no es audio" * 50)
    with pytest.raises(InvalidAudioError):
        LibrosaSegmenter().intervals(tmp_path / "bad.wav")


def test_settings_defaults_and_env():
    assert Settings.from_env({}) == Settings()
    custom = Settings.from_env(
        {
            "SOUPDB_HOST": "db",
            "SOUPDB_PORT": "6000",
            "SOUPDB_POOL_SIZE": "2",
            "COPYRIGHT_SEARCH_K": "9",
            "COPYRIGHT_CORS_ORIGINS": "http://localhost:5173, http://localhost:5173/",
        }
    )
    assert (custom.soupdb_host, custom.soupdb_port, custom.pool_size) == ("db", 6000, 2)
    assert custom.search_k == 9 and custom.cors_origins == ("http://localhost:5173",)


@pytest.mark.parametrize(
    "env",
    [
        {"SOUPDB_PORT": "0"},
        {"SOUPDB_PORT": "abc"},
        {"COPYRIGHT_SEARCH_K": "true"},
        {"SOUPDB_POOL_SIZE": "1.5"},
        {"COPYRIGHT_CORS_ORIGINS": "*"},
    ],
)
def test_invalid_settings_are_errors(env):
    with pytest.raises(ConfigError):
        Settings.from_env(env)


def test_cors_rejects_paths():
    with pytest.raises(ConfigError):
        parse_cors_origins("http://x/app")


@pytest.mark.parametrize(
    ("path", "ok"),
    [
        ("fma/000/000002.mp3", True),
        ("../x.mp3", False),
        ("/abs.mp3", False),
        ("C:/x.mp3", False),
        ("a\\b.mp3", False),
        ("a#b.mp3", False),
        ("a//b.mp3", False),
        ("./a.mp3", False),
    ],
)
def test_relative_audio_paths(path, ok):
    assert is_relative_audio_path(path) is ok


def test_song_cannot_live_under_queries():
    with pytest.raises(ValueError):
        Song(1, "t", "a", "queries/x.mp3", 1.0)


def test_versioned_fma_catalog_is_80_songs_8_genres_480_segments():
    entries = bootstrap.load_catalog(CATALOG)
    doc = json.loads(CATALOG.read_text(encoding="utf-8"))

    assert len(entries) == 80
    assert sum(len(e.segments) for e in entries) == 480
    assert len({s["genre"] for s in doc["songs"]}) == 8
    assert all(e.song.audio_path.startswith("fma/") for e in entries)
    smoke = next(e for e in entries if e.song.song_id == 1482)
    assert (smoke.song.title, smoke.song.artist) == ("Reindeer Dance", "Rainbro")
    assert smoke.segments[0].audio_ref == "fma/001/001482.mp3#segment=000000"


def test_training_manifest_format(tmp_path):
    entries = bootstrap.load_catalog(CATALOG)
    out = tmp_path / "m.jsonl"

    assert bootstrap.write_training_manifest(entries, out) == 480
    rows = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert [r["id"] for r in rows] == sorted(r["id"] for r in rows)
    assert set(rows[0]) == {"id", "type", "path", "segment", "start", "end"}
    assert rows[0]["type"] == "audio_segment"


def test_bootstrap_rejects_inconsistent_catalogs(tmp_path):
    doc = json.loads(CATALOG.read_text(encoding="utf-8"))
    doc["songs"][1]["song_id"] = doc["songs"][0]["song_id"]
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(doc), encoding="utf-8")

    with pytest.raises(bootstrap.BootstrapError):
        bootstrap.load_catalog(bad)


def test_extension_and_index_sql_are_escaped():
    sql = bootstrap.extension_sql("/data/hnsw", "/audio", "/data/o'k.npz", "/data/cache")

    assert sql.startswith("CREATE EXTENSION HNSW WITH (storage_root = '/data/hnsw', profiles = '")
    assert "o''k.npz" in sql
    assert bootstrap.index_sql() == (
        "CREATE INDEX copyright_audio_hnsw ON audio_segments (audio_ref) USING HNSW "
        "WITH METRIC=euclidean, DIMENSION=128, PROFILE='copyright-audio-v1'"
    )
