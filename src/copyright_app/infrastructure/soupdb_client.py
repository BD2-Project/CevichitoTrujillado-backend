"""Cliente TCP del protocolo público de SoupDB (v2), escrito desde ``docs/protocolo.md``.

Es la **única** puerta de este backend hacia la base: no se importa nada del
gestor. Implementa el framing y los payloads de la especificación pública:

Frame (enteros big-endian)::

    magic "SP" (2B) | version (1B) | opcode (1B) | length u32 | payload

Requests: ``PING 0x01``, ``BEGIN 0x03``, ``COMMIT 0x04``, ``ROLLBACK 0x05``,
``QUERY 0x06``, ``PREPARE 0x07``, ``EXECUTE 0x08``, ``DEALLOCATE 0x09``.
Responses: ``PONG 0x02``, ``OK 0x10``, ``RESULT 0x11``, ``ERROR 0x12``.

Valores (tag + datos): ``NULL 0x00``, ``INT 0x01`` (i32), ``FLOAT 0x02`` (f64),
``TEXT 0x03`` (u16 + UTF-8), ``BOOL 0x04`` (1 byte), ``POINT 0x05`` (dos f64),
``BYTES 0x06`` (u32 + bytes; la tabla de tags del documento lo nombra sin dar
su valor, comprobado contra el servidor real en los tests de integración).

Header de columnas v2: ``name``, ``type_name``, ``encoding`` (0 nativo, 1 fijo,
2 variable, 3 opaco), ``size`` y ``length``. Un tipo desconocido llega como bytes.

Una conexión es una sesión del gestor: ``BEGIN``/``COMMIT``/``ROLLBACK`` y los
prepared statements viven en ella. :class:`SoupDBClient` no es thread-safe; para
varios hilos se usa :class:`SoupDBPool` (una conexión por hilo a la vez).
"""

from __future__ import annotations

import itertools
import math
import queue
import socket
import struct
import threading
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass

MAGIC = b"SP"
VERSION = 0x02
HEADER = struct.Struct(">2sBBI")
#: Tope defensivo para un payload recibido (el campo admite 4 GiB).
MAX_PAYLOAD = 256 * 1024 * 1024

OP_PING = 0x01
OP_PONG = 0x02
OP_BEGIN = 0x03
OP_COMMIT = 0x04
OP_ROLLBACK = 0x05
OP_QUERY = 0x06
OP_PREPARE = 0x07
OP_EXECUTE = 0x08
OP_DEALLOCATE = 0x09
OP_OK = 0x10
OP_RESULT = 0x11
OP_ERROR = 0x12

TAG_NULL = 0x00
TAG_INT = 0x01
TAG_FLOAT = 0x02
TAG_TEXT = 0x03
TAG_BOOL = 0x04
TAG_POINT = 0x05
TAG_BYTES = 0x06

ERROR_NAMES = {
    0x00: "GENERIC",
    0x01: "PARSE",
    0x02: "EXECUTION",
    0x03: "TRANSACTION",
    0x04: "LOCK_TIMEOUT",
    0x05: "DEADLOCK",
    0x06: "RECORD_NOT_FOUND",
    0x07: "CONTRACT",
    0x08: "UNSUPPORTED",
}

INT32_MIN, INT32_MAX = -(2**31), 2**31 - 1
U16_MAX = 2**16 - 1


class SoupDBClientError(Exception):
    """Base de los errores del cliente."""


class SoupDBProtocolError(SoupDBClientError):
    """Frame mal formado o inesperado: la conexión deja de ser usable."""


class SoupDBConnectionError(SoupDBClientError):
    """No se pudo conectar, o la conexión se cortó."""


class SoupDBError(SoupDBClientError):
    """El gestor respondió ``ERROR``; la conexión sigue siendo usable."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(f"{ERROR_NAMES.get(code, hex(code))}: {message}")
        self.code = code
        self.code_name = ERROR_NAMES.get(code, "UNKNOWN")
        self.message = message


# --- tipos de resultado ---------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Point:
    x: float
    y: float


@dataclass(frozen=True, slots=True)
class Column:
    name: str
    type_name: str
    encoding: int
    size: int
    length: int


Value = None | int | float | str | bool | Point | bytes


@dataclass(frozen=True, slots=True)
class QueryResult:
    """``RESULT`` (columnas + filas) u ``OK`` (sin columnas, con ``affected``)."""

    columns: tuple[Column, ...] = ()
    rows: tuple[tuple[Value, ...], ...] = ()
    affected: int = 0

    @property
    def column_names(self) -> tuple[str, ...]:
        return tuple(c.name for c in self.columns)

    def dicts(self) -> list[dict[str, Value]]:
        names = self.column_names
        return [dict(zip(names, row, strict=True)) for row in self.rows]


# --- codec ------------------------------------------------------------------------


def encode_frame(opcode: int, payload: bytes = b"", version: int = VERSION) -> bytes:
    return HEADER.pack(MAGIC, version, opcode, len(payload)) + payload


def decode_header(header: bytes) -> tuple[int, int, int]:
    """``(version, opcode, length)`` de un header de 8 bytes."""
    if len(header) != HEADER.size:
        raise SoupDBProtocolError(f"header de {len(header)} bytes")
    magic, version, opcode, length = HEADER.unpack(header)
    if magic != MAGIC:
        raise SoupDBProtocolError(f"magic inválido {magic!r}")
    if version != VERSION:
        raise SoupDBProtocolError(f"versión {version} inesperada (se usa {VERSION})")
    if length > MAX_PAYLOAD:
        raise SoupDBProtocolError(f"payload de {length} bytes supera el tope")
    return version, opcode, length


def _text(value: str, size_format: str) -> bytes:
    data = value.encode("utf-8")
    limit = U16_MAX if size_format == ">H" else 2**32 - 1
    if len(data) > limit:
        raise ValueError(f"texto de {len(data)} bytes; máximo {limit}")
    return struct.pack(size_format, len(data)) + data


def encode_query(sql: str) -> bytes:
    return _text(sql, ">I")


def encode_prepare(name: str, sql: str) -> bytes:
    return _text(name, ">H") + _text(sql, ">I")


def encode_value(value: Value) -> bytes:
    """Un parámetro de ``EXECUTE`` (tag + datos). ``bool`` antes que ``int``."""
    if value is None:
        return bytes([TAG_NULL])
    if isinstance(value, bool):
        return bytes([TAG_BOOL, 1 if value else 0])
    if isinstance(value, int):
        if not INT32_MIN <= value <= INT32_MAX:
            raise ValueError(f"entero fuera de i32: {value}")
        return struct.pack(">Bi", TAG_INT, value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"float no finito: {value!r}")
        return struct.pack(">Bd", TAG_FLOAT, value)
    if isinstance(value, str):
        return bytes([TAG_TEXT]) + _text(value, ">H")
    if isinstance(value, Point):
        return struct.pack(">Bdd", TAG_POINT, value.x, value.y)
    if isinstance(value, bytes | bytearray):
        return struct.pack(">BI", TAG_BYTES, len(value)) + bytes(value)
    raise TypeError(f"tipo de parámetro no soportado: {type(value).__name__}")


def encode_execute(name: str, params: Sequence[Value]) -> bytes:
    if len(params) > U16_MAX:
        raise ValueError("demasiados parámetros")
    out = bytearray(_text(name, ">H"))
    out += struct.pack(">H", len(params))
    for value in params:
        out += encode_value(value)
    return bytes(out)


def encode_deallocate(name: str) -> bytes:
    return _text(name, ">H")


class _Reader:
    """Lectura secuencial y acotada de un payload."""

    def __init__(self, data: bytes) -> None:
        self._data = data
        self._offset = 0

    def take(self, size: int) -> bytes:
        end = self._offset + size
        if end > len(self._data):
            raise SoupDBProtocolError("payload truncado")
        chunk = self._data[self._offset : end]
        self._offset = end
        return chunk

    def unpack(self, fmt: str) -> tuple:
        s = struct.Struct(fmt)
        return s.unpack(self.take(s.size))

    def text(self, size_format: str = ">H") -> str:
        (length,) = self.unpack(size_format)
        try:
            return self.take(length).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SoupDBProtocolError(f"texto no UTF-8: {exc}") from exc

    def done(self) -> None:
        if self._offset != len(self._data):
            raise SoupDBProtocolError(f"{len(self._data) - self._offset} bytes sobrantes")


def decode_value(reader: _Reader) -> Value:
    (tag,) = reader.unpack(">B")
    if tag == TAG_NULL:
        return None
    if tag == TAG_INT:
        return reader.unpack(">i")[0]
    if tag == TAG_FLOAT:
        return reader.unpack(">d")[0]
    if tag == TAG_TEXT:
        return reader.text(">H")
    if tag == TAG_BOOL:
        (flag,) = reader.unpack(">B")
        return flag != 0
    if tag == TAG_POINT:
        x, y = reader.unpack(">dd")
        return Point(x, y)
    if tag == TAG_BYTES:
        (length,) = reader.unpack(">I")
        return reader.take(length)
    raise SoupDBProtocolError(f"tag de valor desconocido 0x{tag:02x}")


def decode_ok(payload: bytes) -> QueryResult:
    reader = _Reader(payload)
    (affected,) = reader.unpack(">I")
    reader.done()
    return QueryResult(affected=affected)


def decode_result(payload: bytes) -> QueryResult:
    reader = _Reader(payload)
    (ncols,) = reader.unpack(">H")
    columns = []
    for _ in range(ncols):
        name = reader.text()
        type_name = reader.text()
        encoding, size, length = reader.unpack(">BHH")
        columns.append(Column(name, type_name, encoding, size, length))
    (nrows,) = reader.unpack(">I")
    rows = tuple(tuple(decode_value(reader) for _ in range(ncols)) for _ in range(nrows))
    reader.done()
    return QueryResult(columns=tuple(columns), rows=rows, affected=nrows)


def decode_error(payload: bytes) -> SoupDBError:
    reader = _Reader(payload)
    (code,) = reader.unpack(">B")
    message = reader.text(">H")
    reader.done()
    return SoupDBError(code, message)


# --- conexión -----------------------------------------------------------------------


class SoupDBClient:
    """Una conexión TCP (una sesión del gestor). No es thread-safe."""

    def __init__(self, host: str, port: int, *, timeout: float = 60.0) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self._sock: socket.socket | None = None
        self._statements: dict[str, str] = {}
        self._names = itertools.count(1)
        self.in_transaction = False

    # ciclo de vida
    def connect(self) -> SoupDBClient:
        if self._sock is None:
            try:
                self._sock = socket.create_connection((self.host, self.port), self.timeout)
            except OSError as exc:
                raise SoupDBConnectionError(
                    f"no se pudo conectar a SoupDB en {self.host}:{self.port}: {exc}"
                ) from exc
            self._sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        return self

    def close(self) -> None:
        sock, self._sock = self._sock, None
        self._statements.clear()
        self.in_transaction = False
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    @property
    def connected(self) -> bool:
        return self._sock is not None

    def __enter__(self) -> SoupDBClient:
        return self.connect()

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # operaciones
    def ping(self) -> None:
        opcode, payload = self._request(OP_PING)
        if opcode != OP_PONG or payload:
            raise self._unexpected(opcode, "PONG")

    def query(self, sql: str) -> QueryResult:
        return self._result(*self._request(OP_QUERY, encode_query(sql)))

    def begin(self) -> None:
        self._result(*self._request(OP_BEGIN))
        self.in_transaction = True

    def commit(self) -> None:
        try:
            self._result(*self._request(OP_COMMIT))
        finally:
            self.in_transaction = False

    def rollback(self) -> None:
        try:
            self._result(*self._request(OP_ROLLBACK))
        finally:
            self.in_transaction = False

    def prepare(self, name: str, sql: str) -> None:
        self._result(*self._request(OP_PREPARE, encode_prepare(name, sql)))

    def execute(self, name: str, params: Sequence[Value] = ()) -> QueryResult:
        return self._result(*self._request(OP_EXECUTE, encode_execute(name, params)))

    def deallocate(self, name: str) -> None:
        self._result(*self._request(OP_DEALLOCATE, encode_deallocate(name)))
        self._statements = {sql: n for sql, n in self._statements.items() if n != name}

    def run(self, sql: str, params: Sequence[Value] = ()) -> QueryResult:
        """Ejecuta ``sql`` con ``$1..$n`` como prepared statement (preparado una vez)."""
        name = self._statements.get(sql)
        if name is None:
            name = f"stmt_{next(self._names)}"
            self.prepare(name, sql)
            self._statements[sql] = name
        return self.execute(name, params)

    @contextmanager
    def transaction(self) -> Iterator[SoupDBClient]:
        """``BEGIN`` … ``COMMIT``; ``ROLLBACK`` si el bloque lanza."""
        self.begin()
        try:
            yield self
        except BaseException:
            if self.connected:
                try:
                    self.rollback()
                except SoupDBClientError:
                    self.close()
            raise
        self.commit()

    # internals
    def _request(self, opcode: int, payload: bytes = b"") -> tuple[int, bytes]:
        sock = self.connect()._sock
        assert sock is not None
        try:
            sock.sendall(encode_frame(opcode, payload))
            _version, reply, length = decode_header(self._recv(sock, HEADER.size))
            return reply, self._recv(sock, length)
        except SoupDBProtocolError:
            self.close()
            raise
        except OSError as exc:
            self.close()
            raise SoupDBConnectionError(f"conexión con SoupDB perdida: {exc}") from exc

    @staticmethod
    def _recv(sock: socket.socket, size: int) -> bytes:
        chunks = bytearray()
        while len(chunks) < size:
            chunk = sock.recv(min(size - len(chunks), 1 << 20))
            if not chunk:
                raise OSError("el servidor cerró la conexión")
            chunks += chunk
        return bytes(chunks)

    def _result(self, opcode: int, payload: bytes) -> QueryResult:
        if opcode == OP_OK:
            return decode_ok(payload)
        if opcode == OP_RESULT:
            return decode_result(payload)
        if opcode == OP_ERROR:
            raise decode_error(payload)
        raise self._unexpected(opcode, "OK/RESULT/ERROR")

    def _unexpected(self, opcode: int, expected: str) -> SoupDBProtocolError:
        self.close()
        return SoupDBProtocolError(f"opcode 0x{opcode:02x} inesperado; se esperaba {expected}")


class SoupDBPool:
    """Conexiones reutilizables; cada hilo toma una en exclusiva.

    Una conexión que falla a nivel de transporte o protocolo se descarta; un
    ``ERROR`` del gestor no la invalida.
    """

    def __init__(self, factory: Callable[[], SoupDBClient], size: int) -> None:
        if type(size) is not int or size <= 0:
            raise ValueError(f"size debe ser un entero positivo, no {size!r}")
        self._factory = factory
        self._idle: queue.LifoQueue[SoupDBClient] = queue.LifoQueue()
        self._slots = threading.BoundedSemaphore(size)
        self._closed = False

    @contextmanager
    def connection(self) -> Iterator[SoupDBClient]:
        if self._closed:
            raise SoupDBConnectionError("el pool está cerrado")
        self._slots.acquire()
        try:
            try:
                client = self._idle.get_nowait()
            except queue.Empty:
                client = self._factory()
            try:
                client.connect()
                yield client
            except BaseException:
                if client.in_transaction and client.connected:
                    try:
                        client.rollback()
                    except SoupDBClientError:
                        client.close()
                raise
            finally:
                if client.connected and not self._closed and not client.in_transaction:
                    self._idle.put(client)
                else:
                    client.close()
        finally:
            self._slots.release()

    def close(self) -> None:
        self._closed = True
        while True:
            try:
                self._idle.get_nowait().close()
            except queue.Empty:
                return
