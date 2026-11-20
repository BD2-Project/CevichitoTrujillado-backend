import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

from copyright_app.api.app import INTERNAL_ERROR, NOT_CONFIGURED, create_app
from copyright_app.errors import InvalidAudioError, ServiceUnavailableError
from copyright_app.infrastructure.audio_store import SharedAudioStore
from copyright_app.schemas import AnalysisResult, CopyrightMatch, SoupDBHealth
from copyright_app.services.library import CatalogLibrary
from tests.support.fakes import InMemoryCatalog, InMemoryHistory, make_entry

ALPHA = make_entry(1, "Alpha", "fma/000/000001.mp3")
FORBIDDEN = ("probab", "is_copyright", "infring", "plagi", "verdict", "detected")


def match(**overrides):
    values = dict(
        query_start=0.0,
        query_end=10.0,
        song_id=1,
        title="Alpha",
        artist="Artista",
        source_start=0.0,
        source_end=10.0,
        score=0.9,
        segment_count=2,
        min_score=0.85,
        max_score=0.95,
        alignment_offset_seconds=0.0,
        is_best_run=True,
    )
    return CopyrightMatch(**{**values, **overrides})


class FakeAnalyzer:
    def __init__(self, outcome):
        self.outcome = outcome

    def analyze(self, upload, filename):
        upload.read()
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


@pytest.fixture
def audio_root(tmp_path):
    (tmp_path / "fma" / "000").mkdir(parents=True)
    (tmp_path / "fma" / "000" / "000001.mp3").write_bytes(b"ID3-mp3")
    return tmp_path


def make_client(audio_root, *, analyzer=None, reachable=True, history=None, **kw):
    return TestClient(
        create_app(
            analyzer=analyzer,
            library=CatalogLibrary(InMemoryCatalog([ALPHA]), SharedAudioStore(audio_root)),
            history=history if history is not None else InMemoryHistory(),
            health_check=lambda: SoupDBHealth(reachable=reachable, latency_ms=1.0),
            **kw,
        ),
        raise_server_exceptions=False,
    )


def post(client, name="q.mp3"):
    return client.post("/api/v1/analyze", files={"file": (name, b"bytes", "audio/mpeg")})


def test_health_reports_soupdb_connectivity(audio_root):
    ok = make_client(audio_root).get("/api/v1/health")
    down = make_client(audio_root, reachable=False).get("/api/v1/health")

    assert ok.status_code == 200
    assert ok.json() == {
        "status": "ok",
        "service": "soupdb-copyright-api",
        "soupdb": {"reachable": True, "latency_ms": 1.0},
    }
    assert down.status_code == 503 and down.json()["status"] == "unavailable"


def test_unconfigured_services_are_503():
    client = TestClient(create_app())

    assert client.get("/api/v1/health").status_code == 503
    assert post(client).json() == {"detail": NOT_CONFIGURED}
    assert client.get("/api/v1/catalog").status_code == 503
    assert client.get("/api/v1/analyses").status_code == 503


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (InvalidAudioError("audio vacío"), 422),
        (ServiceUnavailableError("SoupDB no está disponible"), 503),
        (RuntimeError("secreto /ruta/interna"), 500),
    ],
)
def test_error_mapping(audio_root, error, status):
    response = post(make_client(audio_root, analyzer=FakeAnalyzer(error)))

    assert response.status_code == status
    if status == 500:
        assert response.json() == {"detail": INTERNAL_ERROR}
        assert "secreto" not in response.text
    else:
        assert response.json() == {"detail": str(error)}


def test_analyze_returns_the_result(audio_root):
    result = AnalysisResult(
        analysis_id="a" * 32, filename="q.mp3", segment_seconds=5.0, matches=[match()]
    )

    response = post(make_client(audio_root, analyzer=FakeAnalyzer(result)))

    assert response.status_code == 200
    assert response.json() == result.model_dump(mode="json")


def test_missing_file_is_422(audio_root):
    assert make_client(audio_root).post("/api/v1/analyze").status_code == 422


def test_catalog_and_reference_audio(audio_root):
    client = make_client(audio_root)

    catalog = client.get("/api/v1/catalog").json()
    detail = client.get("/api/v1/catalog/1").json()
    audio = client.get("/api/v1/catalog/1/audio")

    assert (catalog["song_count"], catalog["segment_count"]) == (1, 3)
    assert catalog["songs"][0]["audio_url"] == "/api/v1/catalog/1/audio"
    assert "audio_path" not in catalog["songs"][0]  # sin rutas internas
    assert len(detail["segments"]) == 3
    assert audio.status_code == 200 and audio.content == b"ID3-mp3"
    assert audio.headers["content-type"] == "audio/mpeg"
    assert client.get("/api/v1/catalog/2").status_code == 404
    assert client.get("/api/v1/catalog/0").status_code == 422
    assert client.get("/api/v1/catalog/..%2F..%2Fetc").status_code in (404, 422)


def test_reference_audio_never_leaves_the_root(audio_root, tmp_path):
    store = SharedAudioStore(audio_root)

    assert store.reference_file("../secret.mp3") is None
    assert store.reference_file("queries/" + "a" * 32 + "/candidate.mp3") is None
    assert store.reference_file("fma/000/missing.mp3") is None
    assert store.reference_file("fma/000/000001.mp3") is not None


def test_history_and_exports(audio_root):
    history = InMemoryHistory()
    history.save(
        AnalysisResult(
            analysis_id="b" * 32, filename="=cmd.mp3", segment_seconds=5.0, matches=[match()]
        )
    )
    client = make_client(audio_root, history=history)

    assert [a["analysis_id"] for a in client.get("/api/v1/analyses").json()] == ["b" * 32]
    detail = client.get(f"/api/v1/analyses/{'b' * 32}").json()
    as_json = client.get(f"/api/v1/analyses/{'b' * 32}/export")
    as_csv = client.get(f"/api/v1/analyses/{'b' * 32}/export?format=csv")

    assert as_json.json() == detail
    disposition = as_json.headers["content-disposition"]
    assert disposition == f'attachment; filename="analysis-{"b" * 32}.json"'
    assert as_csv.headers["content-type"].startswith("text/csv")
    assert "'=cmd.mp3" in as_csv.text  # sin inyección de fórmulas
    assert client.get(f"/api/v1/analyses/{'c' * 32}").status_code == 404
    assert client.get(f"/api/v1/analyses/{'b' * 32}/export?format=xml").status_code == 422


def test_cors_only_for_configured_origins(audio_root):
    client = make_client(audio_root, cors_origins=("http://localhost:5173",))
    allowed = client.options(
        "/api/v1/analyze",
        headers={"Origin": "http://localhost:5173", "Access-Control-Request-Method": "POST"},
    )
    denied = client.get("/api/v1/catalog", headers={"Origin": "http://evil.example"})

    assert allowed.headers["access-control-allow-origin"] == "http://localhost:5173"
    assert "access-control-allow-origin" not in denied.headers


def test_openapi_has_no_verdict_or_probability_fields():
    schema = TestClient(create_app()).get("/openapi.json").json()

    assert {"/api/v1/health", "/api/v1/analyze", "/api/v1/catalog"} <= set(schema["paths"])
    for model in schema["components"]["schemas"].values():
        for field in model.get("properties", {}):
            assert not any(word in field.lower() for word in FORBIDDEN), field


def test_importing_the_api_is_light():
    code = (
        "import sys, copyright_app.api.app, copyright_app.services.analysis; "
        "print(sorted(m for m in ('librosa', 'numpy', 'copyright_app.runtime') "
        "if m in sys.modules))"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)

    assert out.stdout.strip() == "[]"
