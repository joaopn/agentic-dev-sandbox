"""Sandbox webui — SSH terminal multiplexer + broker management relay.

Two halves:

* **Terminal half** (unchanged surface): proxies WebSocket connections from
  the browser to SSH sessions inside agent containers (`/tab`), TCP-probes
  services (`/probe`), serves the SPA. No docker socket, no host mounts;
  credential storage lives in the browser vault.

* **Management relay** (`/broker/*`): relays a closed set of lifecycle verbs
  to the host-side broker daemon over its unix socket (RO-mounted run/ dir).
  The browser NEVER sees a broker token — it holds an opaque `sandbox_broker`
  session cookie; the webui maps cookie → broker token in process memory.
  Every state-changing POST is Origin-checked; logins are globally
  rate-limited; long verbs run as background ops tailed via the RO view logs.

The webui cannot import cli/broker.py (that drags docker/yaml into this
image), so `broker_call` below re-implements the length-prefixed JSON framing
async — a MIRROR PAIR with cli/broker.py's client_call, kept honest by the
acceptance runbook.
"""
import asyncio
import contextlib
import ipaddress
import json
import logging
import os
import re
import secrets
import ssl
import struct
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

import asyncssh
from aiohttp import web, WSMsgType
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

import services

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("webui")

STATIC_DIR = Path(__file__).parent / "static"
TLS_DIR = Path(os.environ.get("WEBUI_TLS_DIR", "/app/tls"))
LISTEN_HOST = os.environ.get("WEBUI_HOST", "0.0.0.0")
LISTEN_PORT = int(os.environ.get("WEBUI_PORT", "7777"))
HOST_BIND = os.environ.get("WEBUI_BIND", "127.0.0.1")

# Gitea launcher URL. The webui exposes this via /config; the browser opens
# it in a new tab when the user clicks the leftmost "Gitea" tab. Empty means
# "no Gitea tab" — the launcher tab simply isn't rendered.
GITEA_URL = (os.environ.get("WEBUI_GITEA_URL", "") or "").strip().rstrip("/")

# Upstream DNS prefix for per-project agent containers. Names lockstep with
# the core (sandbox-agent-<project>) and webui/services.py.
AGENT_CONTAINER_PREFIX = "sandbox-agent-"

# Seconds for the /probe + service-probe TCP connect.
TCP_PROBE_TIMEOUT_SECONDS = 3.0

# Project names as they appear in URL path segments. Mirror of the core's
# _PROJECT_NAME_RE (the webui cannot import the core).
PROJECT_NAME_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]*\Z")

# ---------------------------------------------------------------------------
# Broker relay: socket client, sessions, login rate limiting
# ---------------------------------------------------------------------------

# The broker's unix socket, RO-mounted by docker-compose from the host's
# .broker/run/ (PARENT-dir mount — the daemon recreates the socket inode on
# restart; a single-file mount would pin the dead inode). LOCKSTEP: this
# default's directory must equal the compose mount target for BROKER_RUN_DIR.
SANDBOX_BROKER_SOCKET = os.environ.get("SANDBOX_BROKER_SOCKET",
                                       "/run/broker/broker.sock")

# Per-op view logs (allowlist-emitted milestones) live beside the socket in
# the same RO mount; the raw full logs deliberately do NOT (host-only).
BROKER_OPLOG_DIR = Path(SANDBOX_BROKER_SOCKET).parent / "oplogs"
# Broker-owned port-tab registry (written only by the broker's webport verbs).
WEBPORTS_FILE = Path(SANDBOX_BROKER_SOCKET).parent / "registry" / "webports.json"

# 4-byte unsigned big-endian length prefix — mirror of cli/broker.py _LEN.
_BROKER_LEN = struct.Struct(">I")

# Synchronous relay verbs are instant (reads, webport writes, attach); 30 s
# tolerates a momentarily-busy serial daemon without hanging a request.
BROKER_CALL_TIMEOUT_S = 30

# Background-op read timeout. A down broker still fails fast at CONNECT; this
# bound only governs how long a slow verb may run. ADS `create` cold-builds a
# miniconda image and installs the agent CLI, which can far exceed RS's 600s —
# hence 1800 (the plan's one deliberate constants-table exception).
BROKER_OP_TIMEOUT_S = 1800

# Management sessions: opaque browser cookie value -> {token, expires}.
# The broker token never leaves this process; a webui restart logs everyone
# out (matching the broker, whose own TokenStore is also in-memory).
BROKER_SESSIONS: dict[str, dict] = {}
BROKER_COOKIE = "sandbox_broker"
BROKER_SESSION_TTL_SECONDS = 8 * 60 * 60

# op_id names a file in the RO oplog dir; validate as a safe basename before
# it ever does. Mirror of cli/broker.py's _OP_ID_RE.
_OP_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

# Background ops: op_id -> {state: running|ok|failed, result, task,
# broker_token, done_ts}. Completed entries are evicted when the browser
# confirms the terminal state (serve-then-evict in broker_op_status_handler);
# _start_op additionally sweeps terminal entries older than
# BROKER_OP_TIMEOUT_S (abandoned mid-op browser) — so an unclaimed create
# result (which carries ssh_password) is bounded, though only evicted on the
# NEXT op start; an operator who never fires another op keeps it until webui
# restart (accepted tail case).
OP_RUNS: dict[str, dict] = {}

# Global (not per-IP) login rate limit: the webui typically fronts a single
# operator over Tailscale; per-IP book-keeping adds surface without adding
# protection there. 10 failures in a 60s window locks logins for 60s.
LOGIN_MAX_FAILURES = 10
LOGIN_WINDOW_SECONDS = 60
LOGIN_LOCKOUT_SECONDS = 60


class BrokerUnavailable(Exception):
    """The broker socket is absent/unreachable/timing out."""


class BrokerForbidden(Exception):
    """The broker rejected the peer (uid mismatch) — a deployment error."""


class LoginLimiter:
    """Global failed-login limiter. `now` injectable for tests."""

    def __init__(self, max_failures=LOGIN_MAX_FAILURES,
                 window=LOGIN_WINDOW_SECONDS, lockout=LOGIN_LOCKOUT_SECONDS,
                 now=time.time):
        self._max = max_failures
        self._window = window
        self._lockout = lockout
        self._now = now
        self._failures: list[float] = []
        self._locked_until = 0.0

    def retry_after(self) -> int:
        """Seconds until logins are accepted again; 0 if not locked."""
        return max(0, int(self._locked_until - self._now()))

    def record_failure(self) -> None:
        now = self._now()
        self._failures = [t for t in self._failures if now - t < self._window]
        self._failures.append(now)
        if len(self._failures) >= self._max:
            self._locked_until = now + self._lockout
            self._failures = []

    def record_success(self) -> None:
        self._failures = []
        self._locked_until = 0.0


LOGIN_LIMITER = LoginLimiter()


async def broker_call(verb: str, args: dict | None = None, *,
                      token: str | None = None, op_id: str | None = None,
                      timeout: float = BROKER_CALL_TIMEOUT_S) -> dict:
    """One framed request/reply over the broker socket. MIRROR PAIR with
    cli/broker.py client_call — the two framings must stay identical."""
    payload = {"verb": verb, "args": args or {}}
    if token is not None:
        payload["token"] = token
    if op_id is not None:
        payload["op_id"] = op_id
    data = json.dumps(payload).encode()
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(SANDBOX_BROKER_SOCKET), timeout)
    except (FileNotFoundError, ConnectionRefusedError, OSError, asyncio.TimeoutError) as e:
        raise BrokerUnavailable(str(e))
    try:
        writer.write(_BROKER_LEN.pack(len(data)) + data)
        await asyncio.wait_for(writer.drain(), timeout)
        hdr = await asyncio.wait_for(reader.readexactly(_BROKER_LEN.size), timeout)
        (length,) = _BROKER_LEN.unpack(hdr)
        body = await asyncio.wait_for(reader.readexactly(length), timeout)
    except (asyncio.IncompleteReadError, asyncio.TimeoutError, OSError) as e:
        raise BrokerUnavailable(str(e))
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
    reply = json.loads(body)
    if (not reply.get("ok")
            and (reply.get("error") or {}).get("kind") == "forbidden"):
        raise BrokerForbidden((reply["error"].get("message") or "forbidden"))
    return reply


def _broker_session(request: web.Request) -> dict | None:
    """The live management session for this request's cookie, else None.
    Expired entries are GC'd on the way out."""
    cookie = request.cookies.get(BROKER_COOKIE)
    if not cookie:
        return None
    entry = BROKER_SESSIONS.get(cookie)
    if entry is None:
        return None
    if time.time() >= entry["expires"]:
        BROKER_SESSIONS.pop(cookie, None)
        return None
    return entry


def _drop_sessions_for_token(broker_token: str) -> None:
    for cookie, entry in list(BROKER_SESSIONS.items()):
        if entry.get("token") == broker_token:
            BROKER_SESSIONS.pop(cookie, None)


async def _relay(request: web.Request, verb: str, args: dict | None = None, *,
                 timeout: float = BROKER_CALL_TIMEOUT_S) -> web.Response:
    """Session-gated synchronous relay of one broker verb."""
    session = _broker_session(request)
    if session is None:
        return web.json_response({"error": "not logged in"}, status=401)
    try:
        reply = await broker_call(verb, args, token=session["token"],
                                  timeout=timeout)
    except BrokerUnavailable:
        return web.json_response({"error": "broker_unavailable"}, status=503)
    except BrokerForbidden:
        return web.json_response({"error": "forbidden"}, status=403)
    if (not reply.get("ok")
            and (reply.get("error") or {}).get("kind") == "unauthorized"):
        # Broker restarted (token flushed) — drop the stale webui session so
        # the browser re-logs-in instead of looping.
        _drop_sessions_for_token(session["token"])
        return web.json_response(reply, status=401)
    return web.json_response(reply)


def origin_ok(request: web.Request) -> bool:
    """Reject requests whose Origin isn't this server's own. Applied to every
    state-changing POST and the WS handshake; GET reads rely on
    SameSite=Strict cookies."""
    origin = request.headers.get("Origin", "")
    if not origin:
        return False
    parsed = urlparse(origin)
    return parsed.netloc == request.host


# ---------------------------------------------------------------------------
# Broker relay: auth handlers
# ---------------------------------------------------------------------------


async def broker_login_handler(request: web.Request) -> web.Response:
    if not origin_ok(request):
        return web.json_response({"error": "origin rejected"}, status=403)
    retry = LOGIN_LIMITER.retry_after()
    if retry > 0:
        return web.json_response(
            {"error": "too many failed logins", "retry_after": retry},
            status=429, headers={"Retry-After": str(retry)})
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    proof = body.get("proof") if isinstance(body, dict) else None
    if not isinstance(proof, str) or not proof:
        LOGIN_LIMITER.record_failure()
        return web.json_response({"error": "proof required"}, status=401)
    try:
        reply = await broker_call("login", {"proof": proof})
    except BrokerUnavailable:
        return web.json_response({"error": "broker_unavailable"}, status=503)
    except BrokerForbidden:
        return web.json_response({"error": "forbidden"}, status=403)
    if not reply.get("ok"):
        LOGIN_LIMITER.record_failure()
        return web.json_response({"error": "invalid credentials"}, status=401)
    LOGIN_LIMITER.record_success()
    cookie = secrets.token_urlsafe(32)
    BROKER_SESSIONS[cookie] = {
        "token": reply["result"]["token"],
        "expires": time.time() + BROKER_SESSION_TTL_SECONDS,
    }
    resp = web.json_response({"ok": True})
    resp.set_cookie(BROKER_COOKIE, cookie, path="/broker", httponly=True,
                    secure=True, samesite="Strict",
                    max_age=BROKER_SESSION_TTL_SECONDS)
    return resp


async def broker_logout_handler(request: web.Request) -> web.Response:
    if not origin_ok(request):
        return web.json_response({"error": "origin rejected"}, status=403)
    session = _broker_session(request)
    if session is not None:
        # Revoke broker-side too (best effort — logout must always succeed).
        with contextlib.suppress(BrokerUnavailable, BrokerForbidden):
            await broker_call("logout", {}, token=session["token"])
        BROKER_SESSIONS.pop(request.cookies.get(BROKER_COOKIE, ""), None)
    resp = web.json_response({"ok": True, "logged_out": True})
    resp.del_cookie(BROKER_COOKIE, path="/broker")
    return resp


# ---------------------------------------------------------------------------
# Broker relay: reads + synchronous writes
# ---------------------------------------------------------------------------


async def broker_projects_handler(request: web.Request) -> web.Response:
    return await _relay(request, "list")


async def broker_catalog_handler(request: web.Request) -> web.Response:
    return await _relay(request, "catalog")


def _project_from_path(request: web.Request) -> str | None:
    name = request.match_info.get("name", "")
    return name if PROJECT_NAME_RE.match(name) else None


async def broker_attach_handler(request: web.Request) -> web.Response:
    """JIT SSH coordinates (incl. password, in-memory only) for a running
    project. Synchronous — a fast keyring fetch."""
    if not origin_ok(request):
        return web.json_response({"error": "origin rejected"}, status=403)
    name = _project_from_path(request)
    if not name:
        return web.json_response({"error": "invalid project name"}, status=400)
    return await _relay(request, "attach", {"project": name})


async def broker_webports_handler(request: web.Request) -> web.Response:
    name = _project_from_path(request)
    if not name:
        return web.json_response({"error": "invalid project name"}, status=400)
    return await _relay(request, "webport_list", {"project": name})


async def broker_webport_add_handler(request: web.Request) -> web.Response:
    if not origin_ok(request):
        return web.json_response({"error": "origin rejected"}, status=403)
    name = _project_from_path(request)
    if not name:
        return web.json_response({"error": "invalid project name"}, status=400)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    return await _relay(request, "webport_add",
                        {"project": name, "port": body.get("port"),
                         "label": body.get("label")})


async def broker_webport_remove_handler(request: web.Request) -> web.Response:
    if not origin_ok(request):
        return web.json_response({"error": "origin rejected"}, status=403)
    name = _project_from_path(request)
    if not name:
        return web.json_response({"error": "invalid project name"}, status=400)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    return await _relay(request, "webport_remove",
                        {"project": name, "port": body.get("port")})


# ---------------------------------------------------------------------------
# Broker relay: background ops (create + lifecycle actions)
# ---------------------------------------------------------------------------


def _derive_project(url: str) -> str:
    """Repo name from a github URL — MIRROR of the core's parse_project_name
    (cli/sandboxcore.py; the webui cannot import the core). Used only to seed
    the browsable op_id; the broker re-derives authoritatively."""
    name = url.rstrip("/").rsplit("/", 1)[-1]
    if name.endswith(".git"):
        name = name[:-4]
    return name


def _mint_op_id(seed: str, action: str) -> str:
    return f"{seed}-{action}-{int(time.time())}-{secrets.token_hex(4)}"


async def _run_op(op_id: str, verb: str, args: dict, broker_token: str,
                  timeout: float) -> None:
    entry = OP_RUNS[op_id]
    try:
        reply = await broker_call(verb, args, token=broker_token,
                                  op_id=op_id, timeout=timeout)
    except BrokerUnavailable as e:
        reply = {"ok": False, "error": {"kind": "broker_unavailable",
                                        "message": str(e)}}
    except BrokerForbidden as e:
        reply = {"ok": False, "error": {"kind": "forbidden", "message": str(e)}}
    except Exception as e:  # never let a background op die silently
        log.warning(f"op {op_id} ({verb}) failed: {e}")
        reply = {"ok": False, "error": {"kind": "internal", "message": str(e)}}
    if (not reply.get("ok")
            and (reply.get("error") or {}).get("kind") == "unauthorized"):
        _drop_sessions_for_token(broker_token)
    entry["result"] = reply
    entry["state"] = "ok" if reply.get("ok") else "failed"
    entry["done_ts"] = time.time()


def _sweep_abandoned_ops() -> None:
    """Drop terminal entries the browser never claimed. Threshold reuses
    BROKER_OP_TIMEOUT_S: an entry unpolled for longer than the longest
    possible op is abandoned. Bounds how long an unclaimed create result
    (ssh_password included) sits in RAM — evicted on the NEXT op start."""
    now = time.time()
    for op_id, entry in list(OP_RUNS.items()):
        # `or now`: a terminal entry always has done_ts set alongside state
        # today, but guard against a future refactor leaving it None (which
        # would TypeError the subtraction and 500 the next _start_op).
        done_ts = entry.get("done_ts") or now
        if entry.get("state") != "running" and now - done_ts > BROKER_OP_TIMEOUT_S:
            OP_RUNS.pop(op_id, None)


def _start_op(request: web.Request, verb: str, args: dict, *,
              op_seed: str, timeout: float = BROKER_OP_TIMEOUT_S) -> web.Response:
    """Mint an op_id, fire the verb as a background task, return the id
    immediately. The broker stays serial + synchronous; only the webui's read
    await is backgrounded. `op_seed` is REQUIRED (no args-derived default —
    the silent-mis-seed trap)."""
    session = _broker_session(request)
    if session is None:
        return web.json_response({"error": "not logged in"}, status=401)
    _sweep_abandoned_ops()
    op_id = _mint_op_id(op_seed, verb)
    if not _OP_ID_RE.match(op_id):
        return web.json_response({"error": "invalid op id (bad project name?)"},
                                 status=400)
    OP_RUNS[op_id] = {"state": "running", "result": None,
                      "broker_token": session["token"], "done_ts": None}
    OP_RUNS[op_id]["task"] = asyncio.create_task(
        _run_op(op_id, verb, args, session["token"], timeout))
    return web.json_response({"op_id": op_id})


async def broker_create_handler(request: web.Request) -> web.Response:
    if not origin_ok(request):
        return web.json_response({"error": "origin rejected"}, status=403)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "body must be a JSON object"},
                                 status=400)
    # Body forwarded verbatim — the broker's CREATE_WEBUI_FIELDS allowlist is
    # the input boundary, not this handler.
    seed = _derive_project(str(body.get("github_url") or ""))
    return _start_op(request, "create", body, op_seed=seed or "create")


_PROJECT_ACTIONS = frozenset({"start", "stop", "sync", "destroy"})


async def broker_project_action_handler(request: web.Request) -> web.Response:
    if not origin_ok(request):
        return web.json_response({"error": "origin rejected"}, status=403)
    name = _project_from_path(request)
    if not name:
        return web.json_response({"error": "invalid project name"}, status=400)
    action = request.match_info.get("action", "")
    if action not in _PROJECT_ACTIONS:
        return web.json_response({"error": f"unknown action {action!r}"},
                                 status=404)
    args: dict = {"project": name}
    if action == "destroy":
        # Step-up proof rides the request; the broker verifies + consumes it.
        try:
            body = await request.json()
        except Exception:
            body = {}
        if isinstance(body, dict) and isinstance(body.get("proof"), str):
            args["proof"] = body["proof"]
    return _start_op(request, action, args, op_seed=name)


async def broker_op_log_handler(request: web.Request) -> web.Response:
    """Byte-offset tail of an op's RO view log. Missing file → started:false
    (never 404 — the op may not have reached the broker yet)."""
    if _broker_session(request) is None:
        return web.json_response({"error": "not logged in"}, status=401)
    op_id = request.match_info.get("op_id", "")
    if not _OP_ID_RE.match(op_id):
        return web.json_response({"error": "invalid op id"}, status=400)
    try:
        frm = max(0, int(request.query.get("from", "0")))
    except ValueError:
        frm = 0
    path = BROKER_OPLOG_DIR / f"{op_id}.view.log"
    try:
        with open(path, "rb") as f:
            f.seek(frm)
            data = f.read()
            nxt = f.tell()
    except FileNotFoundError:
        return web.json_response({"ok": True, "from": frm, "next": 0,
                                  "data": "", "started": False})
    except OSError as e:
        return web.json_response({"error": f"cannot read op log: {e}"},
                                 status=500)
    return web.json_response({"ok": True, "from": frm, "next": nxt,
                              "data": data.decode("utf-8", "replace"),
                              "started": True})


async def broker_op_status_handler(request: web.Request) -> web.Response:
    """In-memory op state. SERVE-THEN-EVICT: a terminal entry is returned
    once and dropped — the tail loop treats this route as its authoritative
    completion signal and tolerates 'unknown' afterwards. This is what keeps
    a create result's ssh_password from lingering in RAM."""
    if _broker_session(request) is None:
        return web.json_response({"error": "not logged in"}, status=401)
    op_id = request.match_info.get("op_id", "")
    if not _OP_ID_RE.match(op_id):
        return web.json_response({"error": "invalid op id"}, status=400)
    entry = OP_RUNS.get(op_id)
    if entry is None:
        return web.json_response({"state": "unknown"})
    if entry["state"] == "running":
        return web.json_response({"state": "running"})
    OP_RUNS.pop(op_id, None)
    return web.json_response({"state": entry["state"],
                              "result": entry["result"]})


# ---------------------------------------------------------------------------
# Per-project services (tab catalog)
# ---------------------------------------------------------------------------


async def tcp_probe(host: str, port: int) -> bool:
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port),
            timeout=TCP_PROBE_TIMEOUT_SECONDS)
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
        return True
    except (OSError, asyncio.TimeoutError):
        return False


def _read_webports(project: str) -> list[dict]:
    """Tolerant read of the broker-owned registry (RO mount). Mirror of the
    core's reader shape; absent/corrupt → no rows."""
    try:
        data = json.loads(WEBPORTS_FILE.read_text())
    except (OSError, ValueError):
        return []
    rows = data.get(project) if isinstance(data, dict) else None
    return [e for e in (rows or [])
            if isinstance(e, dict) and isinstance(e.get("port"), int)]


async def project_services_handler(request: web.Request) -> web.Response:
    """Tab catalog for one project: the always-on terminal, plus probe-gated
    http entries (editor, registered webports). NOTE: http entries carry no
    origin_url yet — the origin-port proxy stamps that (Stage 4); the frontend
    renders an http tab only once origin_url exists."""
    project = request.match_info.get("project", "")
    if not PROJECT_NAME_RE.match(project):
        return web.json_response({"error": "invalid project name"}, status=400)
    upstream = f"{AGENT_CONTAINER_PREFIX}{project}"

    tabs = []
    for service_id, spec in services.SERVICES.items():
        entry = {"id": service_id, **spec}
        if spec.get("always_on"):
            tabs.append(entry)
        elif await tcp_probe(upstream, spec["default_port"]):
            tabs.append(entry)

    for row in _read_webports(project):
        if await tcp_probe(upstream, row["port"]):
            tabs.append({
                "id": f"webport-{row['port']}",
                "label": row.get("label") or f"Port {row['port']}",
                "kind": "http", "always_on": False, "renderer": "iframe",
                "default_port": row["port"], "upstream_path": "/",
            })

    return web.json_response({"project": project, "services": tabs})


# ---------------------------------------------------------------------------
# SSH terminal bridge (unchanged surface)
# ---------------------------------------------------------------------------


class HostKeyValidator(asyncssh.SSHClient):
    """Capture host key during handshake; reject if it doesn't match expected."""

    def __init__(self, expected_fp: str | None):
        super().__init__()
        self.expected_fp = expected_fp
        self.actual_fp: str | None = None

    def validate_host_public_key(self, host, addr, port, key) -> bool:
        self.actual_fp = key.get_fingerprint("sha256")
        if self.expected_fp is None:
            return True  # TOFU: caller will record what we got
        return self.actual_fp == self.expected_fp


async def ws_handler(request: web.Request) -> web.WebSocketResponse:
    if not origin_ok(request):
        return web.Response(status=403, text="Origin rejected")

    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)

    try:
        first = await asyncio.wait_for(ws.receive(), timeout=10)
    except asyncio.TimeoutError:
        await ws.close()
        return ws

    if first.type != WSMsgType.TEXT:
        await ws.send_json({"type": "error", "msg": "expected JSON connect message"})
        await ws.close()
        return ws

    try:
        connect = json.loads(first.data)
    except json.JSONDecodeError:
        await ws.send_json({"type": "error", "msg": "invalid JSON"})
        await ws.close()
        return ws

    if connect.get("type") != "connect":
        await ws.send_json({"type": "error", "msg": "first message must be type=connect"})
        await ws.close()
        return ws

    host = connect.get("host")
    port = int(connect.get("port", 22))
    username = connect.get("username") or "agent"
    password = connect.get("password")
    expected_fp = connect.get("fingerprint")
    rows = int(connect.get("rows", 24))
    cols = int(connect.get("cols", 80))

    if not host or not password:
        await ws.send_json({"type": "error", "msg": "host and password required"})
        await ws.close()
        return ws

    validator = HostKeyValidator(expected_fp)

    try:
        conn = await asyncssh.connect(
            host=host, port=port,
            username=username, password=password,
            client_factory=lambda: validator,
            known_hosts=None,
            client_keys=None,
            connect_timeout=10,
        )
    except asyncssh.HostKeyNotVerifiable:
        await ws.send_json({"type": "fingerprint_mismatch",
                            "actual": validator.actual_fp})
        await ws.close()
        return ws
    except asyncssh.PermissionDenied:
        await ws.send_json({"type": "auth_failed"})
        await ws.close()
        return ws
    except Exception as e:
        log.warning(f"SSH connect to {host}:{port} failed: {e}")
        await ws.send_json({"type": "error", "msg": f"connect failed: {e}"})
        await ws.close()
        return ws

    await ws.send_json({"type": "connected", "fingerprint": validator.actual_fp})

    try:
        async with conn:
            proc = await conn.create_process(
                term_type="xterm-256color",
                term_size=(cols, rows),
                command="byobu attach -t main 2>/dev/null || byobu new-session -s main -c /home/agent -- bash",
                encoding=None,
            )

            async def from_browser():
                async for msg in ws:
                    if msg.type == WSMsgType.BINARY:
                        proc.stdin.write(msg.data)
                    elif msg.type == WSMsgType.TEXT:
                        try:
                            ctrl = json.loads(msg.data)
                        except json.JSONDecodeError:
                            continue
                        if ctrl.get("type") == "resize":
                            proc.change_terminal_size(
                                width=int(ctrl.get("cols", cols)),
                                height=int(ctrl.get("rows", rows)),
                            )

            async def to_browser():
                while True:
                    chunk = await proc.stdout.read(65536)
                    if not chunk:
                        break
                    if isinstance(chunk, str):
                        chunk = chunk.encode()
                    await ws.send_bytes(chunk)

            done, pending = await asyncio.wait(
                [asyncio.create_task(from_browser()),
                 asyncio.create_task(to_browser())],
                return_when=asyncio.FIRST_COMPLETED,
            )
            for t in pending:
                t.cancel()
            try:
                proc.terminate()
            except OSError:
                pass
    finally:
        if not ws.closed:
            await ws.close()

    return ws


async def probe_handler(request: web.Request) -> web.Response:
    """TCP-connect probe used to color tabs as up/down."""
    host = request.query.get("host", "")
    try:
        port = int(request.query.get("port", "0"))
    except ValueError:
        return web.json_response({"up": False, "error": "invalid port"})
    if not host or port < 1 or port > 65535:
        return web.json_response({"up": False, "error": "host/port required"})
    return web.json_response({"up": await tcp_probe(host, port)})


async def index_handler(request: web.Request) -> web.Response:
    return web.FileResponse(STATIC_DIR / "index.html")


async def config_handler(request: web.Request) -> web.Response:
    """Browser-side runtime config — currently just the Gitea launcher URL."""
    return web.json_response({"gitea_url": GITEA_URL})


# ---------------------------------------------------------------------------
# TLS (unchanged)
# ---------------------------------------------------------------------------


def cert_covers_bind(cert_path: Path, bind: str) -> bool:
    """Check whether the existing cert's SAN already includes `bind`."""
    if not cert_path.exists():
        return False
    try:
        cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
        san = cert.extensions.get_extension_for_class(
            x509.SubjectAlternativeName).value
    except Exception:
        return False
    try:
        bind_ip = ipaddress.ip_address(bind)
        return any(
            isinstance(e, x509.IPAddress) and e.value == bind_ip for e in san
        )
    except ValueError:
        return any(isinstance(e, x509.DNSName) and e.value == bind for e in san)


def generate_self_signed(cert_path: Path, key_path: Path, bind: str) -> None:
    """Write a fresh self-signed cert+key covering localhost and `bind`."""
    log.info(f"Generating self-signed TLS cert at {cert_path} (bind={bind})")
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "sandbox-webui")])

    san_entries = [
        x509.DNSName("localhost"),
        x509.DNSName("sandbox-webui"),
        x509.IPAddress(ipaddress.IPv4Address("127.0.0.1")),
        x509.IPAddress(ipaddress.IPv6Address("::1")),
    ]
    try:
        bind_ip = ipaddress.ip_address(bind)
        if not any(isinstance(e, x509.IPAddress) and e.value == bind_ip
                   for e in san_entries):
            san_entries.append(x509.IPAddress(bind_ip))
    except ValueError:
        if bind not in ("localhost", "sandbox-webui"):
            san_entries.append(x509.DNSName(bind))

    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=825))
        .add_extension(x509.SubjectAlternativeName(san_entries), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(private_key=key, algorithm=hashes.SHA256()))

    cert_path.parent.mkdir(parents=True, exist_ok=True)
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ))
    cert_path.chmod(0o644)
    key_path.chmod(0o600)


def ensure_tls(cert_path: Path, key_path: Path, bind: str) -> ssl.SSLContext:
    if not (cert_path.exists() and key_path.exists()) or not cert_covers_bind(cert_path, bind):
        generate_self_signed(cert_path, key_path, bind)
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))
    return ctx


def main() -> None:
    cert_path = TLS_DIR / "cert.pem"
    key_path = TLS_DIR / "key.pem"
    ssl_ctx = ensure_tls(cert_path, key_path, HOST_BIND)

    # client_max_size=0: management POSTs are tiny, but the default 1 MiB cap
    # is a footgun for anything that grows (RS precedent); no request body
    # here reaches a shell.
    app = web.Application(client_max_size=0)
    app.router.add_get("/", index_handler)
    app.router.add_get("/config", config_handler)
    app.router.add_get("/probe", probe_handler)
    app.router.add_get("/tab", ws_handler)
    app.router.add_get("/services/{project}", project_services_handler)
    # Broker relay. Registration order is load-bearing: fixed segments must
    # precede the {action} catch-all, and /op/{op_id}/log must precede
    # /op/{op_id} (first-match-wins).
    app.router.add_post("/broker/login", broker_login_handler)
    app.router.add_post("/broker/logout", broker_logout_handler)
    app.router.add_get("/broker/projects", broker_projects_handler)
    app.router.add_get("/broker/catalog", broker_catalog_handler)
    app.router.add_post("/broker/project", broker_create_handler)
    app.router.add_post("/broker/project/{name}/attach", broker_attach_handler)
    app.router.add_get("/broker/project/{name}/webports", broker_webports_handler)
    app.router.add_post("/broker/project/{name}/webport", broker_webport_add_handler)
    app.router.add_post("/broker/project/{name}/webport-remove",
                        broker_webport_remove_handler)
    app.router.add_post("/broker/project/{name}/{action}",
                        broker_project_action_handler)
    app.router.add_get("/broker/op/{op_id}/log", broker_op_log_handler)
    app.router.add_get("/broker/op/{op_id}", broker_op_status_handler)
    app.router.add_static("/static", STATIC_DIR)

    log.info(f"Sandbox webui listening on https://{LISTEN_HOST}:{LISTEN_PORT}")
    web.run_app(app, host=LISTEN_HOST, port=LISTEN_PORT,
                ssl_context=ssl_ctx, access_log=log)


if __name__ == "__main__":
    main()
