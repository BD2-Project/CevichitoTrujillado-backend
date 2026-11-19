from pathlib import Path

import pytest

from tests.support.soupdb_server import SoupDBServer, soupdb_python


@pytest.fixture(scope="session")
def soupdb_py() -> Path:
    python = soupdb_python()
    if python is None:
        pytest.skip(
            "no hay intérprete de SoupDB: define SOUPDB_PYTHON (python con soupdb y soupdb-vector)"
        )
    return python


@pytest.fixture
def soupdb_server(soupdb_py, tmp_path):
    """Servidor SoupDB vacío y exclusivo del test."""
    server = SoupDBServer(soupdb_py, tmp_path / "db", plugin_storage=tmp_path / "plugins")
    server.start()
    try:
        yield server
    finally:
        server.stop()
