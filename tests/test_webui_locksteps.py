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
APP = (REPO / "webui" / "static" / "app.js").read_text()


def _const(source: str, name: str) -> str:
    m = re.search(rf'^{name}\s*=\s*(.+?)\s*(#.*)?$', source, re.MULTILINE)
    assert m, f"{name} not found"
    return m.group(1)


def _js_const(name: str) -> str:
    """A top-level `const NAME = <value>;` in app.js (single-line values)."""
    m = re.search(rf'^const {name} = (.+?);', APP, re.MULTILINE)
    assert m, f"const {name} not found in app.js"
    return m.group(1)


def _js_string_list(name: str) -> list[str]:
    """A top-level `const NAME = ["a", "b", ...];` in app.js (may span lines)."""
    m = re.search(rf'^const {name} = \[(.*?)\];', APP, re.MULTILINE | re.DOTALL)
    assert m, f"const {name} not found in app.js"
    return re.findall(r'"([^"]+)"', m.group(1))


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


# ─── app.js legs (Stage 3b frontend) ─────────────────────────────────────────


def test_appjs_login_proof_mirror():
    """§9 row 1: LOGIN_PROOF_SALT_STR / LOGIN_PROOF_ITERATIONS (app.js) must
    equal LOGIN_PROOF_SALT / LOGIN_PROOF_ITERATIONS (cli/broker_auth.py) —
    drift means every login fails. The derivation itself is anchored by the
    pinned vector in tests/test_broker_auth.py + the acceptance runbook."""
    from cli import broker_auth
    salt = _js_const("LOGIN_PROOF_SALT_STR").strip('"')
    assert salt.encode() == broker_auth.LOGIN_PROOF_SALT
    assert int(_js_const("LOGIN_PROOF_ITERATIONS")) == broker_auth.LOGIN_PROOF_ITERATIONS


def test_appjs_create_fields_mirror():
    """§9 row 2 (3-leg): the create dialog builds its POST body by iterating
    CREATE_FIELDS; the broker silently drops anything outside
    CREATE_WEBUI_FIELDS, so a drifted name is a silently-lost field."""
    from cli import broker
    fields = _js_string_list("CREATE_FIELDS")
    assert len(fields) == len(set(fields)), "duplicate field in CREATE_FIELDS"
    assert set(fields) == set(broker.CREATE_WEBUI_FIELDS)


def test_appjs_webport_fields_mirror():
    """The Add-port-tab payload names must be broker-accepted (project rides
    in the URL path, injected server-side)."""
    from cli import broker
    fields = _js_string_list("WEBPORT_FIELDS")
    assert set(fields) <= set(broker.WEBPORT_ADD_FIELDS)
    assert "project" not in fields


def test_appjs_op_checklists_match_progress_keys():
    """§9 row 3: OP_CHECKLISTS keys (app.js) must equal the core verbs'
    progress.step() keys per verb, order included — drift means
    stuck-pending checklist rows in the op box."""
    m = re.search(r'^const OP_CHECKLISTS = \{(.*?)\n\};', APP,
                  re.MULTILINE | re.DOTALL)
    assert m, "OP_CHECKLISTS not found in app.js"
    # Non-greedy to the first `]`: verb arrays hold flat {key, label} objects
    # (no nested arrays), so the first `]` after `verb: [` closes the block.
    blocks = dict(re.findall(r'(\w+): \[(.*?)\]', m.group(1), re.DOTALL))
    assert set(blocks) == set(sandboxcore.PROGRESS_KEYS)
    for verb, body in blocks.items():
        keys = re.findall(r'key: "([^"]+)"', body)
        assert keys == sandboxcore.PROGRESS_KEYS[verb], (
            f"OP_CHECKLISTS[{verb}] keys {keys} != PROGRESS_KEYS "
            f"{sandboxcore.PROGRESS_KEYS[verb]}")


def test_appjs_vault_key_and_password_floor():
    """§9 row 6: the vault-create floor mirrors broker_auth.MIN_PASSWORD_LENGTH
    (one master password must satisfy both). VAULT_KEY stays the pre-3b name
    so existing vaults survive the frontend rewrite."""
    from cli import broker_auth
    assert _js_const("VAULT_KEY") == '"sandbox-webui-vault"'
    assert int(_js_const("MIN_PASSWORD_LENGTH")) == broker_auth.MIN_PASSWORD_LENGTH


def test_appjs_step_up_mapping_keys_on_kind():
    """The 'Wrong password.' mapping must key on the error KIND, never on
    message text (the broker's step-up message is free to change)."""
    assert 'kind === "step_up_required"' in APP
