"""Framing round-trip against a real _Server on a scratch unix socket:
length-prefixed JSON both ways, frame-size cap, malformed-body envelopes,
0600 socket mode. Verbs are stubbed — no docker is touched."""

import json
import socket
import stat
import struct
import threading

import pytest

from cli import broker, broker_auth

_LEN = struct.Struct(">I")


def _echo_verb(args, progress=None):
    return {"echo": args}


@pytest.fixture
def server(scratch_dir, monkeypatch):
    """Real _Server + _Handler on a scratch socket, with a stub verb table:
    `echo` is open, `gated` requires a token. Audit is silenced."""
    monkeypatch.setattr(broker, "VERBS",
                        {"echo": _echo_verb, "gated": _echo_verb})
    monkeypatch.setattr(broker, "OPEN_VERBS", frozenset({"echo"}))
    monkeypatch.setattr(broker_auth, "audit_event",
                        lambda *a, **kw: None)
    sock_path = scratch_dir / "b.sock"
    srv = broker._Server(str(sock_path), broker._Handler)
    srv.tokens = broker_auth.TokenStore()
    thread = threading.Thread(target=srv.serve_forever,
                              kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    yield sock_path
    srv.shutdown()
    srv.server_close()
    thread.join(timeout=5)


def _raw_exchange(sock_path, payload: bytes, *, header: bytes = None) -> dict:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(5)
        s.connect(str(sock_path))
        s.sendall((header if header is not None else _LEN.pack(len(payload)))
                  + payload)
        hdr = broker._recv_exact(s, 4)
        (length,) = _LEN.unpack(hdr)
        return json.loads(broker._recv_exact(s, length))


def test_round_trip(server):
    reply = broker.client_call("echo", {"x": 1}, socket_path=server)
    assert reply == {"ok": True, "result": {"echo": {"x": 1}}}


def test_gated_verb_over_the_wire_unauthorized(server):
    reply = broker.client_call("gated", {}, socket_path=server)
    assert reply["error"]["kind"] == "unauthorized"


def test_unknown_verb_over_the_wire(server):
    reply = broker.client_call("nope", {}, socket_path=server)
    assert reply["error"]["kind"] == "unknown_verb"


def test_oversize_frame_rejected_before_body(server):
    # Header alone declares an over-cap body; the reply must arrive without
    # the body ever being sent.
    reply = _raw_exchange(server, b"",
                          header=_LEN.pack(broker.MAX_REQUEST_BYTES + 1))
    assert reply["error"]["kind"] == "bad_request"
    assert "too large" in reply["error"]["message"]


def test_invalid_json_bad_request(server):
    reply = _raw_exchange(server, b"{oops")
    assert reply["error"]["kind"] == "bad_request"
    assert "invalid JSON" in reply["error"]["message"]


def test_non_object_request_bad_request(server):
    reply = _raw_exchange(server, json.dumps("just a string").encode())
    assert reply["error"]["kind"] == "bad_request"
    assert "JSON object" in reply["error"]["message"]


def test_socket_mode_0600(server):
    assert stat.S_IMODE(server.stat().st_mode) == 0o600


def test_token_flows_over_the_wire(server, monkeypatch):
    monkeypatch.setattr(broker_auth, "verify_password",
                        lambda pw, **kw: pw == "proof")
    reply = broker.client_call("login", {"proof": "proof"}, socket_path=server)
    token = reply["result"]["token"]
    reply = broker.client_call("gated", {"y": 2}, token=token,
                               socket_path=server)
    assert reply == {"ok": True, "result": {"echo": {"y": 2}}}
    broker.client_call("logout", {}, token=token, socket_path=server)
    reply = broker.client_call("gated", {}, token=token, socket_path=server)
    assert reply["error"]["kind"] == "unauthorized"
