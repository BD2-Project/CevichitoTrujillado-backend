"""Prueba el bootstrap, el análisis por HTTP y el reinicio de SoupDB."""

import csv
import io
import json
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from copyright_app import bootstrap
from copyright_app.infrastructure.segmentation import intervals_for_length
from copyright_app.runtime import build_app
from copyright_app.settings import Settings
from tests.support.audio import SAMPLE_RATE, write_wav
from tests.support.soupdb_server import SoupDBServer, run_codebook_training

pytestmark = pytest.mark.e2e

SONGS = (
    (1, "Alpha", "Demo Ensemble", "synth/alpha.wav", 20.0, 220.0, 11),
    (2, "Beta", "Demo Ensemble", "synth/beta.wav", 15.0, 520.0, 22),
    (3, "Gamma", "Demo Ensemble", "synth/gamma.wav", 12.5, 1200.0, 33),
)


def catalog_document(audio_root: Path) -> dict:
    songs, next_id = [], 1
    for song_id, title, artist, rel, seconds, base_hz, seed in SONGS:
        write_wav(audio_root / rel, seconds, base_hz, seed)
        intervals = intervals_for_length(round(seconds * SAMPLE_RATE))
        songs.append(
            {
                "song_id": song_id,
                "title": title,
                "artist": artist,
                "audio_path": rel,
                "duration_seconds": round(seconds * SAMPLE_RATE) / SAMPLE_RATE,
                "first_segment_id": next_id,
                "segments": [[i.index, i.start_seconds, i.end_seconds] for i in intervals],
            }
        )
        next_id += 100
    return {
        "version": 1,
        "segment_seconds": 5.0,
        "song_count": len(songs),
        "segment_count": sum(len(s["segments"]) for s in songs),
        "songs": songs,
    }


@pytest.fixture(scope="module")
def deployment(soupdb_py, tmp_path_factory):
    base = tmp_path_factory.mktemp("e2e")
    audio, artifacts = base / "audio", base / "artifacts"
    catalog_path = base / "catalog.json"
    catalog_path.write_text(json.dumps(catalog_document(audio)), encoding="utf-8")
    manifest = artifacts / "training_manifest.jsonl"
    assert (
        bootstrap.main(
            [
                "training-manifest",
                "--catalog",
                str(catalog_path),
                "--audio-root",
                str(audio),
                "--output",
                str(manifest),
                "--verify-audio",
            ]
        )
        == 0
    )
    run_codebook_training(
        soupdb_py, manifest, audio, base / "cache", artifacts / "codebook.npz", base
    )
    server = SoupDBServer(soupdb_py, base / "db", plugin_storage=base / "plugins").start()
    deployment = {
        "base": base,
        "audio": audio,
        "catalog": catalog_path,
        "server": server,
        "args": [
            "catalog",
            "--catalog",
            str(catalog_path),
            "--storage-root",
            str(base / "hnsw"),
            "--profile-root",
            str(audio),
            "--codebook",
            str(artifacts / "codebook.npz"),
            "--cache",
            str(base / "cache"),
        ],
    }
    try:
        yield deployment
    finally:
        if deployment["server"].process is not None:
            deployment["server"].stop()


def settings_for(deployment) -> Settings:
    server = deployment["server"]
    return Settings(
        soupdb_host=server.host,
        soupdb_port=server.port,
        audio_root=deployment["audio"],
        search_k=5,
    )


def run_bootstrap(deployment, monkeypatch, capsys) -> dict:
    monkeypatch.setenv("SOUPDB_HOST", deployment["server"].host)
    monkeypatch.setenv("SOUPDB_PORT", str(deployment["server"].port))
    assert bootstrap.main(deployment["args"]) == 0
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def analyze(client, path: Path, name: str):
    with path.open("rb") as audio:
        return client.post("/api/v1/analyze", files={"file": (name, audio, "audio/wav")})


def test_full_flow_then_restart(deployment, monkeypatch, capsys):
    # 1. Bootstrap por TCP: tablas, filas, extensión y HNSW.
    summary = run_bootstrap(deployment, monkeypatch, capsys)
    assert summary == {
        "songs": 3,
        "segments": 10,
        "inserted_songs": 3,
        "extension_created": True,
        "index_created": True,
    }
    # Repetirlo no cambia nada.
    again = run_bootstrap(deployment, monkeypatch, capsys)
    assert again["inserted_songs"] == 0 and not again["index_created"]
    assert not again["extension_created"]

    # 2. API real sobre SoupDB por TCP.
    query = deployment["base"] / "upload" / "remix.wav"
    query.parent.mkdir()
    shutil.copy(deployment["audio"] / "synth" / "alpha.wav", query)
    with TestClient(build_app(settings_for(deployment))) as client:
        health = client.get("/api/v1/health")
        assert health.status_code == 200 and health.json()["soupdb"]["reachable"] is True

        response = analyze(client, query, "../../remix.wav")
        assert response.status_code == 200, response.text
        body = response.json()
        top = body["matches"][0]
        assert body["filename"] == "remix.wav"
        assert (top["title"], top["is_best_run"], top["segment_count"]) == ("Alpha", True, 4)
        assert (top["query_start"], top["query_end"]) == (0.0, 20.0)
        assert (top["source_start"], top["source_end"]) == (0.0, 20.0)
        assert top["score"] == pytest.approx(1.0, abs=1e-6)
        assert not (deployment["audio"] / "queries" / body["analysis_id"]).exists()
        audio_url = body["audio_url"]
        assert client.get(audio_url).content == query.read_bytes()
        partial = client.get(audio_url, headers={"Range": "bytes=0-31"})
        assert partial.status_code == 206
        assert partial.content == query.read_bytes()[:32]

        listed = client.get("/api/v1/analyses").json()
        assert [a["analysis_id"] for a in listed] == [body["analysis_id"]]
        detail = client.get(f"/api/v1/analyses/{body['analysis_id']}").json()
        assert detail["matches"] == body["matches"]
        exported = client.get(f"/api/v1/analyses/{body['analysis_id']}/export?format=csv")
        rows = list(csv.DictReader(io.StringIO(exported.text)))
        assert rows[0]["title"] == "Alpha" and len(rows) == len(body["matches"])
        assert detail.pop("audio_url") == audio_url
        assert client.get(f"/api/v1/analyses/{body['analysis_id']}/export").json() == detail

        catalog = client.get("/api/v1/catalog").json()
        assert (catalog["song_count"], catalog["segment_count"]) == (3, 10)
        audio = client.get("/api/v1/catalog/1/audio")
        assert audio.content == (deployment["audio"] / "synth" / "alpha.wav").read_bytes()
        assert client.get("/api/v1/catalog/999/audio").status_code == 404

        short = deployment["base"] / "upload" / "short.wav"
        write_wav(short, 1.0, 300.0, 5)
        assert analyze(client, short, "short.wav").status_code == 422

    # 3. Reinicio ordenado de SoupDB: historial y HNSW se reabren desde disco/SysPlugins.
    old = deployment["server"]
    old.stop()
    restarted = SoupDBServer(old.python, old.data_dir, plugin_storage=old.plugin_storage)
    restarted.port = old.port
    deployment["server"] = restarted.start()
    with TestClient(build_app(settings_for(deployment))) as client:
        assert client.get(audio_url).content == query.read_bytes()
        assert [a["analysis_id"] for a in client.get("/api/v1/analyses").json()] == [
            body["analysis_id"]
        ]
        again = analyze(client, query, "remix.wav").json()
        assert again["matches"][0]["title"] == "Alpha"
        assert again["matches"][0]["score"] == pytest.approx(top["score"], abs=1e-9)
        assert len(client.get("/api/v1/analyses").json()) == 2

    # 4. SoupDB caído: health 503 y las peticiones dan 503 (no 500).
    app = build_app(settings_for(deployment))
    with TestClient(app) as client:
        deployment["server"].stop()
        assert client.get("/api/v1/health").status_code == 503
        assert client.get("/api/v1/analyses").status_code == 503
