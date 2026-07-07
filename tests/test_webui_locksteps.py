"""Text-level pins for webui ↔ host mirror pairs.

The webui sources are NOT host-importable (webui/server.py drags
aiohttp/asyncssh in; the host can't have them), so these tests pin the §9
lockstep constants by parsing the source bytes. Regex-on-source is fragile
by nature — if one of these fails after a benign rename, fix the REGEX only
after confirming the VALUE still matches its mirror leg.
"""

import re
from pathlib import Path

from cli import sandboxcore

REPO = Path(__file__).resolve().parent.parent
SERVER = (REPO / "webui" / "server.py").read_text()
SERVICES = (REPO / "webui" / "services.py").read_text()
COMPOSE = (REPO / "docker-compose.yml").read_text()


def _const(source: str, name: str) -> str:
    m = re.search(rf'^{name}\s*=\s*(.+?)\s*(#.*)?$', source, re.MULTILINE)
    assert m, f"{name} not found"
    return m.group(1)


def test_broker_cookie_name():
    assert _const(SERVER, "BROKER_COOKIE") == '"sandbox_broker"'


def test_broker_socket_default_matches_compose_mount():
    """3-leg path lockstep: server.py default ↔ compose mount target
    (↔ the BROKER_RUN_DIR export in cmd_webui, covered by the runbook)."""
    m = re.search(r'SANDBOX_BROKER_SOCKET\s*=\s*os\.environ\.get\(\s*"SANDBOX_BROKER_SOCKET",\s*\n?\s*"([^"]+)"\)', SERVER)
    assert m, "SANDBOX_BROKER_SOCKET default not found"
    socket_default = m.group(1)
    assert socket_default == "/run/broker/broker.sock"
    assert ":/run/broker:ro" in COMPOSE
    assert "SANDBOX_BROKER_SOCKET=/run/broker/broker.sock" in COMPOSE


def test_broker_op_timeout():
    assert _const(SERVER, "BROKER_OP_TIMEOUT_S") == "1800"


def test_derive_project_mirror_exists():
    """server.py must carry the parse_project_name mirror (it can't import
    the core); behavior is pinned end-to-end by the acceptance runbook's
    op-id assertion."""
    assert "def _derive_project(" in SERVER
    # Same two operations as core.parse_project_name: last path segment,
    # .git suffix strip.
    assert 'rsplit("/", 1)[-1]' in SERVER
    assert '.endswith(".git")' in SERVER


def test_project_name_regex_mirror():
    m = re.search(r'PROJECT_NAME_RE\s*=\s*re\.compile\(r"([^"]+)"\)', SERVER)
    assert m and m.group(1) == sandboxcore._PROJECT_NAME_RE.pattern


def test_op_id_regex_mirror():
    from cli import broker
    m = re.search(r'_OP_ID_RE\s*=\s*re\.compile\(r"([^"]+)"\)', SERVER)
    assert m and m.group(1) == broker._OP_ID_RE.pattern


def test_services_names_lockstep():
    """Container/user/session names: sandbox-agent-<p> upstream, byobu
    session 'main', /home/agent cwd — drift means dead terminal tabs."""
    assert '"sandbox-agent-"' in SERVER          # AGENT_CONTAINER_PREFIX
    assert "byobu attach -t main" in SERVICES
    assert "byobu new-session -s main -c /home/agent" in SERVICES
    # The catalog command must equal the /tab bridge's command.
    m = re.search(r'command="([^"]+)"', SERVER)
    assert m, "/tab byobu command not found in server.py"
    m2 = re.search(r'"command": \("([^"]+)"\n?\s*" ([^"]+)"\)', SERVICES)
    assert m2, "terminal command not found in services.py"
    assert m.group(1) == m2.group(1) + " " + m2.group(2)


def test_login_limiter_values():
    assert _const(SERVER, "LOGIN_MAX_FAILURES") == "10"
    assert _const(SERVER, "LOGIN_WINDOW_SECONDS") == "60"
    assert _const(SERVER, "LOGIN_LOCKOUT_SECONDS") == "60"
