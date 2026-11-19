"""Lanzador de ``python -m engine`` para los tests (lo ejecuta el intérprete de SoupDB).

Este archivo **no** se importa desde los tests: se pasa como script al Python
del entorno de SoupDB. Solo añade una parada ordenada en Windows: ``CTRL_BREAK``
se convierte en ``KeyboardInterrupt``, así ``python -m engine`` cierra el
``Catalog`` en su ``finally`` (en Linux basta ``SIGTERM``, que ya maneja el gestor).
"""

import runpy
import signal


def _stop(*_args: object) -> None:
    raise KeyboardInterrupt


if hasattr(signal, "SIGBREAK"):
    signal.signal(signal.SIGBREAK, _stop)
runpy.run_module("engine", run_name="__main__", alter_sys=True)
