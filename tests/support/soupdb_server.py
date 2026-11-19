"""Servidor SoupDB real como **proceso externo** para tests de integración y E2E.

Los tests no importan nada del gestor: arrancan ``python -m engine`` con el
intérprete de un entorno donde SoupDB y el plugin ``soupdb-vector`` están
instalados, y hablan con él por TCP.

- ``SOUPDB_PYTHON``: intérprete de SoupDB. Por defecto, el venv del repositorio
  hermano ``../SoupBD/.venv``. Si no existe, los tests que lo necesitan se
  marcan como *skip* con el motivo explícito.
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LAUNCHER = Path(__file__).with_name("launch_soupdb.py")
_DEFAULT_PYTHONS = (
    ROOT.parent / "SoupBD" / ".venv" / "Scripts" / "python.exe",
    ROOT.parent / "SoupBD" / ".venv" / "bin" / "python",
    ROOT.parent / "SoupDB" / ".venv" / "Scripts" / "python.exe",
    ROOT.parent / "SoupDB" / ".venv" / "bin" / "python",
)


def soupdb_python() -> Path | None:
    configured = os.environ.get("SOUPDB_PYTHON")
    if configured:
        return Path(configured) if Path(configured).is_file() else None
    return next((p for p in _DEFAULT_PYTHONS if p.is_file()), None)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class SoupDBServer:
    """``python -m engine`` en un puerto libre, con datos en ``data_dir``."""

    def __init__(self, python: Path, data_dir: Path, *, plugin_storage: Path) -> None:
        self.python = python
        self.data_dir = data_dir
        self.plugin_storage = plugin_storage
        self.host = "127.0.0.1"
        self.port = free_port()
        self.process: subprocess.Popen[str] | None = None
        self.log = data_dir.parent / f"soupdb-{self.port}.log"

    def start(self, timeout: float = 60.0) -> SoupDBServer:
        env = {
            **os.environ,
            "SOUPDB_DATA": str(self.data_dir),
            "DRIVER_HOST": self.host,
            "DRIVER_PORT": str(self.port),
            "SOUPDB_PLUGIN_STORAGE": str(self.plugin_storage),
            "PYTHONIOENCODING": "utf-8",
        }
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
        self._log_handle = self.log.open("a", encoding="utf-8")
        self.process = subprocess.Popen(
            [str(self.python), str(LAUNCHER)],
            cwd=str(self.data_dir.parent),
            env=env,
            stdout=self._log_handle,
            stderr=subprocess.STDOUT,
            creationflags=flags,
        )
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError(f"SoupDB terminó al arrancar:\n{self.log.read_text()}")
            try:
                socket.create_connection((self.host, self.port), 0.5).close()
                return self
            except OSError:
                time.sleep(0.1)
        self.kill()
        raise RuntimeError("SoupDB no abrió el puerto a tiempo")

    def stop(self, timeout: float = 30.0) -> int | None:
        """Parada ordenada: el gestor cierra el ``Catalog`` (vuelca páginas a disco)."""
        if self.process is None:
            return None
        if self.process.poll() is None:
            if sys.platform == "win32":
                self.process.send_signal(signal.CTRL_BREAK_EVENT)
                # accept() bloqueante no ve la señal hasta que llega una conexión.
                time.sleep(0.2)
                try:
                    socket.create_connection((self.host, self.port), 1).close()
                except OSError:
                    pass
            else:
                self.process.terminate()
            try:
                self.process.wait(timeout)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        code = self.process.returncode
        self.process = None
        self._log_handle.close()
        return code

    def kill(self) -> None:
        if self.process is not None:
            self.process.kill()
            self.process.wait()
            self.process = None
            self._log_handle.close()


def run_codebook_training(
    python: Path, manifest: Path, audio_root: Path, cache: Path, codebook: Path, work: Path
) -> None:
    """Entrena el codebook K=128 con el CLI oficial del plugin (proceso de SoupDB)."""
    subprocess.run(
        [
            str(python),
            "-m",
            "soupdb_vector.multimedia.build_vectors",
            str(manifest),
            str(audio_root),
            str(cache),
            str(codebook),
            str(work / "vectores.bin"),
            "--k",
            "128",
            "--seed",
            "0",
        ],
        check=True,
        capture_output=True,
        text=True,
        cwd=str(work),
    )
