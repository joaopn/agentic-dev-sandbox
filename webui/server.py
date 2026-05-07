"""Sandbox webui — SSH terminal multiplexer.

Proxies WebSocket connections from the browser to SSH sessions inside agent
containers. No docker socket, no host mounts, no agent-side changes.
All credential storage lives in the browser.
"""
import asyncio
import ipaddress
import json
import logging
import os
import ssl
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

import asyncssh
from aiohttp import web, WSMsgType
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


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


def origin_ok(request: web.Request) -> bool:
    """Reject WS handshakes whose Origin isn't this server's own."""
    origin = request.headers.get("Origin", "")
    if not origin:
        return False
    parsed = urlparse(origin)
    return parsed.netloc == request.host


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

    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=3)
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return web.json_response({"up": True})
    except (OSError, asyncio.TimeoutError):
        return web.json_response({"up": False})


async def index_handler(request: web.Request) -> web.Response:
    return web.FileResponse(STATIC_DIR / "index.html")


async def config_handler(request: web.Request) -> web.Response:
    """Browser-side runtime config — currently just the Gitea launcher URL."""
    return web.json_response({"gitea_url": GITEA_URL})


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

    app = web.Application()
    app.router.add_get("/", index_handler)
    app.router.add_get("/config", config_handler)
    app.router.add_get("/probe", probe_handler)
    app.router.add_get("/tab", ws_handler)
    app.router.add_static("/static", STATIC_DIR)

    log.info(f"Sandbox webui listening on https://{LISTEN_HOST}:{LISTEN_PORT}")
    web.run_app(app, host=LISTEN_HOST, port=LISTEN_PORT,
                ssl_context=ssl_ctx, access_log=log)


if __name__ == "__main__":
    main()
