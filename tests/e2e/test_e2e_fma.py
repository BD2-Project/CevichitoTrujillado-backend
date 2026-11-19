"""E2E real con audio FMA: smoke test conocido ``Reindeer Dance / Rainbro``.

Catálogo FMA pequeño (la primera canción de cada uno de los 8 géneros del
catálogo versionado, que incluye la pista 1482) copiado a un ``/audio`` temporal,
codebook K=128 entrenado con el CLI oficial de ``soupdb-vector``, HNSW creado por
SQL remoto y análisis por la API.

Necesita FMA Small en disco: ``FMA_AUDIO_ROOT`` (carpeta con ``000/``, ``001/``…;
por defecto ``D:/datasets/fma/fma_small``). Si no existe, el test se salta con el
motivo explícito; el dataset (7.2 GiB) nunca se versiona.
"""

import json
import os
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from copyright_app import bootstrap
from copyright_app.runtime import build_app
from copyright_app.settings import Settings
from tests.support.soupdb_server import SoupDBServer, run_codebook_training

pytestmark = pytest.mark.e2e

CATALOG = Path(__file__).resolve().parents[2] / "data" / "fma_small_catalog.json"
SMOKE_TRACK = 1482  # Reindeer Dance / Rainbro


def fma_root() -> Path | None:
    root = Path(os.environ.get("FMA_AUDIO_ROOT", "D:/datasets/fma/fma_small"))
    return root if (root / "001" / "001482.mp3").is_file() else None


@pytest.fixture(scope="module")
def fma_deployment(soupdb_py, tmp_path_factory):
    source = fma_root()
    if source is None:
        pytest.skip("FMA Small no disponible: define FMA_AUDIO_ROOT")
    base = tmp_path_factory.mktemp("fma")
    audio = base / "audio"
    doc = json.loads(CATALOG.read_text(encoding="utf-8"))
    chosen, genres = [], set()
    for song in doc["songs"]:
        if song["genre"] not in genres:
            genres.add(song["genre"])
            chosen.append(song)
    assert SMOKE_TRACK in {s["song_id"] for s in chosen} and len(chosen) == 8
    for song in chosen:
        target = audio / song["audio_path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(source / song["audio_path"].removeprefix("fma/"), target)
    subset = {
        **doc,
        "songs": chosen,
        "song_count": len(chosen),
        "segment_count": sum(len(s["segments"]) for s in chosen),
    }
    catalog = base / "catalog.json"
    catalog.write_text(json.dumps(subset), encoding="utf-8")
    manifest = base / "artifacts" / "training_manifest.jsonl"
    assert (
        bootstrap.main(
            [
                "training-manifest",
                "--catalog",
                str(catalog),
                "--audio-root",
                str(audio),
                "--output",
                str(manifest),
                "--verify-audio",
            ]
        )
        == 0
    )
    codebook = base / "artifacts" / "codebook.npz"
    run_codebook_training(soupdb_py, manifest, audio, base / "cache", codebook, base)
    server = SoupDBServer(soupdb_py, base / "db", plugin_storage=base / "plugins").start()
    try:
        os.environ["SOUPDB_HOST"], os.environ["SOUPDB_PORT"] = server.host, str(server.port)
        assert (
            bootstrap.main(
                [
                    "catalog",
                    "--catalog",
                    str(catalog),
                    "--storage-root",
                    str(base / "hnsw"),
                    "--profile-root",
                    str(audio),
                    "--codebook",
                    str(codebook),
                    "--cache",
                    str(base / "cache"),
                ]
            )
            == 0
        )
        yield {"audio": audio, "server": server, "source": source, "songs": chosen}
    finally:
        os.environ.pop("SOUPDB_HOST", None)
        os.environ.pop("SOUPDB_PORT", None)
        server.stop()


def test_reindeer_dance_smoke(fma_deployment):
    server = fma_deployment["server"]
    settings = Settings(
        soupdb_host=server.host, soupdb_port=server.port, audio_root=fma_deployment["audio"]
    )
    sample = fma_deployment["source"] / "001" / "001482.mp3"
    with TestClient(build_app(settings)) as client:
        catalog = client.get("/api/v1/catalog").json()
        assert catalog["song_count"] == 8
        with sample.open("rb") as audio:
            response = client.post(
                "/api/v1/analyze", files={"file": ("sample_query.mp3", audio, "audio/mpeg")}
            )
        assert response.status_code == 200, response.text
        body = response.json()
        top = body["matches"][0]
        assert (top["title"], top["artist"], top["song_id"]) == ("Reindeer Dance", "Rainbro", 1482)
        assert top["is_best_run"] is True
        assert top["segment_count"] == 6  # 0–30 s continuo: 6 bloques de 5 s
        assert top["query_start"] == top["source_start"] == 0.0
        assert top["alignment_offset_seconds"] == 0.0
        assert top["score"] == pytest.approx(1.0, abs=1e-6)
        others = [m for m in body["matches"] if m["song_id"] != SMOKE_TRACK]
        assert all(m["segment_count"] < 6 for m in others)

        history = client.get("/api/v1/analyses").json()
        assert history[0]["analysis_id"] == body["analysis_id"]
        csv_export = client.get(f"/api/v1/analyses/{body['analysis_id']}/export?format=csv")
        assert "Reindeer Dance" in csv_export.text
        original = client.get(f"/api/v1/catalog/{SMOKE_TRACK}/audio")
        assert original.headers["content-type"] == "audio/mpeg"
        assert original.content == sample.read_bytes()
