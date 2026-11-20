"""Convierte CTRL_BREAK en KeyboardInterrupt para cerrar el catálogo."""

import runpy
import signal


def _stop(*_args: object) -> None:
    raise KeyboardInterrupt


if hasattr(signal, "SIGBREAK"):
    signal.signal(signal.SIGBREAK, _stop)
runpy.run_module("engine", run_name="__main__", alter_sys=True)
