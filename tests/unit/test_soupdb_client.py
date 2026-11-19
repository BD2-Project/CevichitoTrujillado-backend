"""Codec y cliente TCP del protocolo v2, sin SoupDB: bytes de la especificación y un
servidor falso en un hilo."""

import socket
import struct
import threading

import pytest

from copyright_app.infrastructure import soupdb_client as proto
from copyright_app.infrastructure.soupdb_client import (
    Column,
    Point,
    SoupDBClient,
    SoupDBConnectionError,
    SoupDBError,
    SoupDBPool,
    SoupDBProtocolError,
)

# --- frames y payloads (ejemplos escritos a mano desde docs/protocolo.md) ---------------


def test_frame_header_is_magic_version_opcode_length():
    assert proto.encode_frame(proto.OP_PING) == b"SP\x02\x01\x00\x00\x00\x00"
    assert proto.encode_frame(proto.OP_QUERY, b"abc") == b"SP\x02\x06\x00\x00\x00\x03abc"


def test_query_and_prepare_payloads():
    assert proto.encode_query("SELECT 1") == b"\x00\x00\x00\x08SELECT 1"
    assert proto.encode_prepare("s1", "X") == b"\x00\x02s1\x00\x00\x00\x01X"
    assert proto.encode_deallocate("s1") == b"\x00\x02s1"


@pytest.mark.parametrize(
    ("value", "encoded"),
    [
        (None, b"\x00"),
        (7, b"\x01\x00\x00\x00\x07"),
        (-1, b"\x01\xff\xff\xff\xff"),
        (0.5, b"\x02" + struct.pack(">d", 0.5)),
        ("ñ", b"\x03\x00\x02\xc3\xb1"),
        (True, b"\x04\x01"),
        (False, b"\x04\x00"),
        (Point(1.0, 2.0), b"\x05" + struct.pack(">dd", 1.0, 2.0)),
        (b"\x01\x02", b"\x06\x00\x00\x00\x02\x01\x02"),
    ],
)
def test_value_encoding(value, encoded):
    assert proto.encode_value(value) == encoded


@pytest.mark.parametrize("bad", [2**31, -(2**31) - 1, float("nan"), float("inf"), object()])
def test_invalid_parameters_are_rejected(bad):
    with pytest.raises((ValueError, TypeError)):
        proto.encode_value(bad)


def test_execute_payload():
    assert proto.encode_execute("s", (1, "a")) == (
        b"\x00\x01s" + b"\x00\x02" + b"\x01\x00\x00\x00\x01" + b"\x03\x00\x01a"
    )


def result_payload() -> bytes:
    def text(s: str) -> bytes:
        return struct.pack(">H", len(s.encode())) + s.encode()

    header = struct.pack(">H", 3)
    header += text("id") + text("INT") + struct.pack(">BHH", 0, 4, 0)
    header += text("name") + text("TEXT") + struct.pack(">BHH", 0, 0, 0)
    header += text("vec") + text("VECTOR") + struct.pack(">BHH", 3, 0, 0)
    rows = struct.pack(">I", 2)
    rows += b"\x01\x00\x00\x00\x01" + b"\x03" + text("a") + b"\x06\x00\x00\x00\x01\xff"
    rows += b"\x00" + b"\x00" + b"\x00"
    return header + rows


def test_result_decoding_keeps_v2_types_and_opaque_values():
    result = proto.decode_result(result_payload())

    assert result.columns == (
        Column("id", "INT", 0, 4, 0),
        Column("name", "TEXT", 0, 0, 0),
        Column("vec", "VECTOR", 3, 0, 0),
    )
    assert result.rows == ((1, "a", b"\xff"), (None, None, None))
    assert result.dicts()[0] == {"id": 1, "name": "a", "vec": b"\xff"}


def test_ok_and_error_decoding():
    assert proto.decode_ok(b"\x00\x00\x00\x05").affected == 5
    error = proto.decode_error(b"\x01\x00\x03bad")
    assert (error.code, error.code_name, error.message) == (1, "PARSE", "bad")


@pytest.mark.parametrize(
    "payload", [b"\x00", result_payload()[:-1], result_payload() + b"x", b"\x00\x01\x00"]
)
def test_malformed_payloads_raise_protocol_errors(payload):
    with pytest.raises(SoupDBProtocolError):
        proto.decode_result(payload)


@pytest.mark.parametrize(
    "header",
    [b"XX\x02\x10\x00\x00\x00\x00", b"SP\x01\x10\x00\x00\x00\x00", b"SP\x02\x10\xff\xff\xff\xff"],
)
def test_bad_headers(header):
    with pytest.raises(SoupDBProtocolError):
        proto.decode_header(header)


# --- cliente contra un servidor falso ---------------------------------------------------------


class FakeServer:
    """Responde según la especificación: registra cada request recibido."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.requests: list[tuple[int, bytes]] = []
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen()
        self.port = self.sock.getsockname()[1]
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        conn, _ = self.sock.accept()
        with conn:
            for reply in self.replies:
                header = conn.recv(8, socket.MSG_WAITALL)
                if len(header) < 8:
                    return
                _, _, opcode, length = struct.unpack(">2sBBI", header)
                payload = conn.recv(length, socket.MSG_WAITALL) if length else b""
                self.requests.append((opcode, payload))
                if reply is None:
                    return  # corta la conexión
                conn.sendall(reply)

    def close(self):
        self.sock.close()


def ok(n=0):
    return proto.encode_frame(proto.OP_OK, struct.pack(">I", n))


def test_client_operations_round_trip():
    server = FakeServer(
        [
            proto.encode_frame(proto.OP_PONG),
            ok(),
            ok(),
            proto.encode_frame(proto.OP_RESULT, result_payload()),
            ok(),
            ok(),
            ok(1),
            proto.encode_frame(proto.OP_ERROR, b"\x02\x00\x04boom"),
        ]
    )
    with SoupDBClient("127.0.0.1", server.port, timeout=5) as client:
        client.ping()
        client.begin()
        client.prepare("s", "SELECT $1")
        assert client.execute("s", (1,)).rows[0][0] == 1
        client.commit()
        assert client.run("INSERT INTO t VALUES ($1)", (2,)).affected == 1
        with pytest.raises(SoupDBError, match="EXECUTION: boom"):
            client.query("SELECT * FROM t")
    server.close()

    opcodes = [op for op, _ in server.requests]
    assert opcodes == [
        proto.OP_PING,
        proto.OP_BEGIN,
        proto.OP_PREPARE,
        proto.OP_EXECUTE,
        proto.OP_COMMIT,
        proto.OP_PREPARE,
        proto.OP_EXECUTE,
        proto.OP_QUERY,
    ]
    assert server.requests[-1][1] == proto.encode_query("SELECT * FROM t")


def test_unexpected_opcode_closes_the_connection():
    server = FakeServer([proto.encode_frame(proto.OP_OK, b"\x00\x00\x00\x00")])
    client = SoupDBClient("127.0.0.1", server.port, timeout=5).connect()
    with pytest.raises(SoupDBProtocolError, match="PONG"):
        client.ping()
    assert not client.connected
    server.close()


def test_dropped_connection_is_a_connection_error():
    server = FakeServer([None])
    client = SoupDBClient("127.0.0.1", server.port, timeout=5)
    with pytest.raises(SoupDBConnectionError):
        client.query("SELECT 1")
    assert not client.connected
    server.close()


def test_unreachable_server():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    with pytest.raises(SoupDBConnectionError, match="no se pudo conectar"):
        SoupDBClient("127.0.0.1", port, timeout=1).connect()


def test_transaction_context_rolls_back_on_error():
    server = FakeServer([ok(), ok(1), ok()])
    client = SoupDBClient("127.0.0.1", server.port, timeout=5)
    with pytest.raises(RuntimeError), client.transaction():
        client.query("INSERT INTO t VALUES (1)")
        raise RuntimeError("x")
    assert [op for op, _ in server.requests] == [proto.OP_BEGIN, proto.OP_QUERY, proto.OP_ROLLBACK]
    assert not client.in_transaction
    client.close()
    server.close()


class StubClient:
    def __init__(self):
        self.connected = False
        self.in_transaction = False
        self.closed = 0

    def connect(self):
        self.connected = True
        return self

    def close(self):
        self.connected = False
        self.closed += 1

    def rollback(self):
        self.in_transaction = False


def test_pool_reuses_and_discards_connections():
    created = []

    def factory():
        created.append(StubClient())
        return created[-1]

    pool = SoupDBPool(factory, 2)
    with pool.connection() as first:
        pass
    with pool.connection() as again:
        assert again is first
        again.connected = False  # conexión rota: se descarta
    with pool.connection() as fresh:
        assert fresh is not first
    with pytest.raises(ValueError), pool.connection() as client:
        client.in_transaction = True
        raise ValueError
    assert client.in_transaction is False  # se hizo rollback y vuelve al pool
    pool.close()
    with pytest.raises(SoupDBConnectionError), pool.connection():
        pass
    with pytest.raises(ValueError):
        SoupDBPool(factory, 0)
