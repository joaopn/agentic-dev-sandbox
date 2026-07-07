"""sandboxcore — importable lifecycle core for the LLM Agent Sandbox.

Holds the lifecycle verbs (create/destroy/start/stop/sync/status/list/attach,
webport registry) as functions over validated request dataclasses that return
result dataclasses, plus the substrate helpers they are built from — one
validator and one implementation shared by the CLI (sandbox.py) and the future
broker daemon. stdlib only (PyYAML is the one permitted extra dep repo-wide;
this module currently needs none of it).

Error channels (the broker dispatch contract), exactly three:
  * ValidationError  — bad request input, raised before any side effect.
  * HarnessError     — a secret-adjacent step failed after side effects began;
                       carries a step name for the durable log and a
                       token-scrubbed detail for the client.
  * die()/SystemExit — mid-execution abort with a terminal-facing message.
Catching anything else is the Stage 2 dispatcher's job, not the verbs'.

Print policy: chatty mid-verb step prints stay HERE (the broker tees verb
stdout into its host-only full log); interactive prompts and final result
formatting live in sandbox.py. Verbs never call input().
"""

import base64
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

# Repo root (this file lives in <repo>/cli/).
SCRIPT_DIR = Path(__file__).resolve().parent.parent

GITEA_INTERNAL_URL = "http://sandbox-gitea:3000"

PORT_FORWARDER_IMAGE = "sandbox-router:latest"

# ─── Broker state (webport registry) ─────────────────────────────────────────
# Repo-local, gitignored, host-only (0700) — follows the .ci-watch/ precedent.
# run/ is the only subdir ever mounted (RO) into the webui container.
BROKER_DIR = SCRIPT_DIR / ".broker"
BROKER_RUN_DIR = BROKER_DIR / "run"
WEBPORTS_FILE = BROKER_RUN_DIR / "registry" / "webports.json"

# Webport tabs proxy to ports the agent serves inside its container: never
# privileged ports (the agent runs unprivileged; <1024 can only be noise or a
# spoof attempt), never above the TCP port space.
WEBPORT_PORT_MIN = 1024
WEBPORT_PORT_MAX = 65535
# A tab strip stays legible at ~8 tabs; an agent needing more should serve one
# index page instead.
WEBPORTS_PER_PROJECT_CAP = 8
# Labels render in the SPA tab strip: no markup, no control chars; 32 chars
# fits a tab without truncation.
_WEBPORT_LABEL_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9 ._-]{0,31}\Z")


# ─── Error channels ───────────────────────────────────────────────────────────


class ValidationError(ValueError):
    """Bad request input. Always raised before any side effect."""


class HarnessError(Exception):
    """A secret-adjacent step failed AFTER side effects began. Carries TWO
    fields so the broker dispatch can split the sinks: ``log_msg`` (step name
    only) for the durable log, and ``client_detail`` (token-scrubbed error
    text) for the client error envelope. The CLI prints ``client_detail``."""

    def __init__(self, log_msg: str, client_detail: str = ""):
        super().__init__(log_msg)
        self.log_msg = log_msg
        self.client_detail = client_detail


def die(msg: str) -> None:
    print(f"Error: {msg}", file=sys.stderr)
    sys.exit(1)


# ─── Subprocess helpers ───────────────────────────────────────────────────────


def run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    """Run a command, printing on failure."""
    return subprocess.run(cmd, **kwargs)


def run_check(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    """Run a command, exit on failure."""
    r = subprocess.run(cmd, capture_output=True, text=True, **kwargs)
    if r.returncode != 0:
        die(f"Command failed: {' '.join(cmd)}\n{r.stderr}")
    return r


def run_quiet(cmd: list[str]) -> bool:
    """Run a command, return True if it succeeded."""
    return subprocess.run(cmd, capture_output=True).returncode == 0


# ─── Configuration ────────────────────────────────────────────────────────────


class Config:
    """Loaded from .env."""

    def __init__(self):
        self.github_pat = ""
        self.gitea_admin_token = ""
        self.gitea_admin_password = ""
        self.projects_dir = ""
        self.gitea_port = "3000"
        self.default_memory = ""
        self.default_open_egress = False
        self.default_profile = ""
        self.dns_servers: list[str] = []
        self.ci_watch_enabled = False
        self.ci_watch_poll_interval = 5
        self.ci_watch_gitea_token = ""


def load_config() -> Config:
    cfg = Config()

    # Load .env
    env_file = SCRIPT_DIR / ".env"
    if not env_file.exists():
        die(".env not found. Copy .env.example to .env and fill in values.")
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, _, value = line.partition("=")
            os.environ[key.strip()] = value.strip()

    cfg.github_pat = os.environ.get("GITHUB_PAT", "")
    cfg.gitea_admin_token = os.environ.get("GITEA_ADMIN_TOKEN", "")
    cfg.gitea_admin_password = os.environ.get("GITEA_ADMIN_PASSWORD", "")
    cfg.gitea_port = os.environ.get("GITEA_PORT", "3000")
    cfg.projects_dir = os.environ.get("PROJECTS_DIR", "./container_volumes/")
    dns = os.environ.get("SANDBOX_DNS", "")
    if not dns.strip():
        die("SANDBOX_DNS not set in .env. Example: SANDBOX_DNS=9.9.9.9,149.112.112.112")
    cfg.dns_servers = [s.strip() for s in dns.split(",") if s.strip()]

    # CI Watch settings
    cfg.ci_watch_enabled = os.environ.get("CI_WATCH_ENABLED", "").lower() in ("true", "1", "yes")
    try:
        cfg.ci_watch_poll_interval = int(os.environ.get("CI_WATCH_POLL_INTERVAL", "5"))
    except ValueError:
        cfg.ci_watch_poll_interval = 5
    cfg.ci_watch_gitea_token = os.environ.get("CI_WATCH_GITEA_TOKEN", "")

    # Resolve relative projects_dir to absolute
    if cfg.projects_dir:
        cfg.projects_dir = str((SCRIPT_DIR / cfg.projects_dir).resolve())

    return cfg


# ─── Gitea helpers ────────────────────────────────────────────────────────────


def gitea_api(cfg: Config, method: str, path: str, body: dict | None = None) -> dict | list | str:
    """Call the Gitea API. Returns parsed JSON or raw string."""
    url = f"http://localhost:{cfg.gitea_port}/api/v1{path}"
    headers = {
        "Authorization": f"token {cfg.gitea_admin_token}",
        "Content-Type": "application/json",
    }
    data = json.dumps(body).encode() if body else None
    req = Request(url, data=data, headers=headers, method=method)
    try:
        with urlopen(req, timeout=30) as resp:
            content = resp.read().decode()
            ct = resp.headers.get("Content-Type", "")
            if "application/json" in ct and content:
                return json.loads(content)
            return content
    except (HTTPError, URLError) as e:
        raise RuntimeError(f"Gitea API {method} {path}: {e}") from e


def gitea_api_ok(cfg: Config, method: str, path: str, body: dict | None = None) -> bool:
    """Call Gitea API, return True if successful."""
    try:
        gitea_api(cfg, method, path, body)
        return True
    except RuntimeError:
        return False


def gitea_api_or(cfg: Config, method: str, path: str, default, body: dict | None = None):
    """Call Gitea API, return default on failure."""
    try:
        return gitea_api(cfg, method, path, body)
    except RuntimeError:
        return default


def http_basic_auth_post(url: str, username: str, password: str, body: dict) -> dict:
    """POST with HTTP Basic Auth, return parsed JSON."""
    credentials = base64.b64encode(f"{username}:{password}".encode()).decode()
    headers = {
        "Authorization": f"Basic {credentials}",
        "Content-Type": "application/json",
    }
    req = Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
    with urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def http_basic_auth_request(url: str, username: str, password: str,
                            method: str = "GET", body: dict | None = None) -> dict | list:
    """HTTP request with Basic Auth, return parsed JSON."""
    credentials = base64.b64encode(f"{username}:{password}".encode()).decode()
    headers = {"Authorization": f"Basic {credentials}", "Content-Type": "application/json"}
    data = json.dumps(body).encode() if body else None
    req = Request(url, data=data, headers=headers, method=method)
    with urlopen(req, timeout=30) as resp:
        content = resp.read().decode()
        return json.loads(content) if content.strip() else {}


def wait_for_gitea(cfg: Config, timeout: int = 120) -> None:
    print("Waiting for Gitea to be ready...")
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            req = Request(f"http://localhost:{cfg.gitea_port}/api/v1/version")
            with urlopen(req, timeout=5):
                print("Gitea is ready.")
                return
        except (URLError, OSError):
            time.sleep(2)
    die(f"Gitea did not become ready within {timeout}s")


def gen_password() -> str:
    return secrets.token_urlsafe(16)


def generate_gitea_token(cfg: Config, gitea_user: str, user_pass: str) -> str:
    """Delete old tokens and create a fresh one for a Gitea user. Shared by create and recreate."""
    base = f"http://localhost:{cfg.gitea_port}/api/v1/users/{gitea_user}/tokens"

    # List and delete existing tokens (using basic auth as the user)
    try:
        existing = http_basic_auth_request(base, gitea_user, user_pass)
        if isinstance(existing, list):
            for tok in existing:
                if "id" in tok:
                    http_basic_auth_request(
                        f"{base}/{tok['id']}", gitea_user, user_pass, method="DELETE")
    except (HTTPError, URLError):
        pass  # No tokens to delete

    resp = http_basic_auth_request(base, gitea_user, user_pass, method="POST", body={
        "name": "agent-token",
        "scopes": [
            "write:repository",   # git push/pull
            "write:issue",        # issues, PRs, comments, labels, merges
            "read:misc",          # API discovery
            "read:user",          # user info for auth
            "read:notification",  # notifications
        ],
    })
    token = resp.get("sha1") or resp.get("token") or ""
    if not token:
        die(f"Failed to generate Gitea token for {gitea_user}: {resp}")
    return token


def _scrub_secrets(text: str, cfg: Config) -> str:
    """Mask any configured secret that leaked into error text before it enters
    a HarnessError client_detail (broker replies must never carry the PAT or
    Gitea credentials)."""
    for secret in (cfg.github_pat, cfg.gitea_admin_token,
                   cfg.gitea_admin_password, cfg.ci_watch_gitea_token):
        if secret:
            text = text.replace(secret, "***")
    return text


# ─── Naming / profile helpers ─────────────────────────────────────────────────


def find_free_port(base: int = 2222) -> int:
    for port in range(base, base + 1000):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(("", port))
                return port
        except OSError:
            continue
    die(f"Could not find free port in range {base}-{base + 1000}")


def parse_project_name(url: str) -> str:
    name = url.rstrip("/").rsplit("/", 1)[-1]
    if name.endswith(".git"):
        name = name[:-4]
    return name


def list_agents() -> list[str]:
    """Return available agent types (subdirectories of container/)."""
    container_dir = SCRIPT_DIR / "container"
    return sorted(p.name for p in container_dir.iterdir()
                  if p.is_dir() and not p.name.startswith("."))


def resolve_profile_image(profile: str) -> tuple[str, Path]:
    """Return (image_tag, dockerfile_path) for a given profile name."""
    dockerfile = SCRIPT_DIR / "agent" / f"Dockerfile.{profile}"
    if not dockerfile.exists():
        available = sorted(
            p.name.removeprefix("Dockerfile.")
            for p in (SCRIPT_DIR / "agent").glob("Dockerfile.*")
            if not p.name.endswith(".sh")
        )
        raise ValidationError(f"Unknown profile '{profile}'. Available: {', '.join(available)}")
    image_tag = f"sandbox-agent-{profile}:latest"
    return image_tag, dockerfile


# Profiles that need GPU passthrough by default.
_GPU_PROFILES = {"cuda"}


def profile_default_gpus(profile: str) -> str:
    """Return default --gpus value for a profile (empty string means none)."""
    return "all" if profile in _GPU_PROFILES else ""


# ─── Docker query primitives ──────────────────────────────────────────────────


def get_agent_containers() -> list[str]:
    r = subprocess.run(
        ["docker", "ps", "-a", "--filter", "name=^sandbox-agent-", "--format", "{{.Names}}"],
        capture_output=True, text=True,
    )
    return [n for n in r.stdout.strip().splitlines() if n]


def container_exists(name: str) -> bool:
    r = subprocess.run(
        ["docker", "ps", "-a", "--format", "{{.Names}}", "--filter", f"name=^{name}$"],
        capture_output=True, text=True,
    )
    return r.returncode == 0 and bool(r.stdout.strip())


def container_running(name: str) -> bool:
    r = subprocess.run(
        ["docker", "inspect", "-f", "{{.State.Running}}", name],
        capture_output=True, text=True,
    )
    return r.stdout.strip() == "true"


def sysbox_available() -> bool:
    """Check if sysbox-runc is registered as a Docker runtime."""
    r = subprocess.run(
        ["docker", "info", "--format", "{{json .Runtimes}}"],
        capture_output=True, text=True,
    )
    return "sysbox-runc" in r.stdout


# ─── Agent install / byobu ────────────────────────────────────────────────────


def start_byobu_session(container_name: str) -> None:
    """Start (or restart) the byobu session inside the agent container.

    Called after all post-start configuration (Docker install, Claude Code, etc.)
    so the shell inherits the final environment (supplementary groups, PATH, etc.).

    Uses 'su - agent' so that PAM resolves supplementary groups from the
    container's current /etc/group (docker exec alone does not pick up groups
    added via usermod after container creation).
    """
    run(["docker", "exec", container_name, "bash", "-c",
         "byobu kill-session -t main 2>/dev/null || true"],
        capture_output=True)
    run_check(["docker", "exec", "-d", "-u", "0", container_name,
               "su", "-", "agent", "-c",
               "byobu new-session -d -s main -c /home/agent -- bash"])


def install_claude_code(container_name: str) -> None:
    """Install Claude Code inside the agent container (synchronous, needs network)."""
    r = subprocess.run(
        ["docker", "exec", container_name, "bash", "-c",
         '[[ -x "$HOME/.local/bin/claude" ]]'],
        capture_output=True,
    )
    if r.returncode == 0:
        print("Claude Code already installed, skipping.")
        return
    print("Installing Claude Code (this may take a minute)...")
    run_check([
        "docker", "exec", container_name, "bash", "-c",
        'export PATH="$HOME/.local/bin:$PATH" && curl -fsSL https://claude.ai/install.sh | bash',
    ])
    print("Claude Code installed.")


def install_goose(container_name: str) -> None:
    """Install Goose CLI inside the agent container (synchronous, needs network)."""
    r = subprocess.run(
        ["docker", "exec", container_name, "bash", "-c",
         '[[ -x "$HOME/.local/bin/goose" ]]'],
        capture_output=True,
    )
    if r.returncode == 0:
        print("Goose already installed, skipping.")
        return
    print("Installing Goose dependencies...")
    run_check([
        "docker", "exec", container_name, "bash", "-c",
        "sudo apt-get update -qq && sudo apt-get install -y -qq libgomp1 >/dev/null",
    ])
    print("Installing Goose (this may take a minute)...")
    run_check([
        "docker", "exec", container_name, "bash", "-c",
        'export PATH="$HOME/.local/bin:$PATH"'
        " && curl -fsSL https://github.com/block/goose/releases/download/stable/download_cli.sh"
        " | CONFIGURE=false bash",
    ])
    print("Goose installed.")


# Agent CLIs installable into a container. CreateRequest validates against this
# at pre-flight so a missing installer fails before any side effect.
INSTALLERS = {
    "claude": install_claude_code,
    "goose": install_goose,
}


def install_agent(agent_type: str, container_name: str) -> None:
    """Install the chosen agent CLI inside the container."""
    installer = INSTALLERS.get(agent_type)
    if not installer:
        die(f"No installer for agent '{agent_type}'.")
    installer(container_name)


def install_docker_dind(container_name: str) -> None:
    """Install Docker CE inside a Sysbox agent container and start dockerd."""
    r = subprocess.run(
        ["docker", "exec", container_name, "bash", "-c", "command -v dockerd"],
        capture_output=True,
    )
    if r.returncode == 0:
        print("Docker already installed, skipping.")
        return
    print("Installing Docker-in-Docker (this may take a minute)...")
    run_check([
        "docker", "exec", container_name, "bash", "-c",
        "curl -fsSL https://get.docker.com | sudo sh",
    ])
    # Use crun instead of runc as the inner OCI runtime.  runc's procfs mount
    # validation (CVE-2025-52881) false-positives on sysbox-fs FUSE-emulated
    # /proc/sys.  crun implements the same OCI spec (namespaces, cgroups, seccomp,
    # capabilities) without this issue.  https://github.com/nestybox/sysbox/issues/973
    run_check([
        "docker", "exec", container_name, "bash", "-c",
        "sudo apt-get install -y crun"
        " && sudo mkdir -p /etc/docker"
        ' && echo \'{"default-runtime":"crun","runtimes":{"crun":{"path":"/usr/bin/crun"}}}\''
        " | sudo tee /etc/docker/daemon.json >/dev/null",
    ])
    run_check([
        "docker", "exec", container_name, "bash", "-c",
        "sudo usermod -aG docker agent",
    ])
    # Start dockerd (entrypoint already ran, so we start it manually on first install)
    run_check([
        "docker", "exec", container_name, "bash", "-c",
        "sudo dockerd > /tmp/dockerd.log 2>&1 & "
        "for i in $(seq 1 30); do docker info >/dev/null 2>&1 && break; sleep 1; done",
    ])
    print("Docker-in-Docker installed and running.")


def build_agent_docker_args(
    *,
    container_name: str,
    project: str,
    network: str,
    volume_name: str,
    ssh_port: int,
    agent_token: str,
    gitea_user: str,
    ssh_pass: str,
    dns_servers: list[str],
    memory: str,
    open_egress: bool,
    image: str,
    branch: str = "",
    cpus: str = "",
    gpus: str = "",
    agent_type: str = "",
    docker: bool = False,
    ci_watch: bool = False,
) -> list[str]:
    """Build the docker run argument list. Shared by create and recreate."""
    dns_args = []
    for s in dns_servers:
        dns_args += ["--dns", s]
    args = [
        "run", "-d",
        "--name", container_name,
        "--network", network,
        "--hostname", project,
        *dns_args,
        "-v", f"{volume_name}:/home/agent",
        "-p", f"{ssh_port}:22",
        "-e", f"GITEA_URL={GITEA_INTERNAL_URL}",
        "-e", f"GITEA_TOKEN={agent_token}",
        "-e", f"GITEA_USER={gitea_user}",
        "-e", f"REPO_NAME={project}",
        "-e", f"SSH_PASSWORD={ssh_pass}",
        "-e", f"HOST_GID={os.getgid()}",
        "--label", f"sandbox.project={project}",
        "--label", f"sandbox.egress={open_egress}",
    ]
    if docker:
        # Sysbox isolates via user namespaces — skip capability dropping.
        # Capabilities inside are real but scoped to a namespace with no host effect.
        args += ["--runtime=sysbox-runc"]
        args += ["-e", "DOCKER_DIND=true"]
        args += ["--pids-limit=2048"]
    else:
        # Standard hardening: drop all caps, re-add only what's needed
        args += [
            "--cap-drop=ALL",
            "--cap-add=CHOWN", "--cap-add=DAC_OVERRIDE", "--cap-add=FOWNER",
            "--cap-add=SETGID", "--cap-add=SETUID", "--cap-add=KILL",
            "--cap-add=FSETID", "--cap-add=AUDIT_WRITE", "--cap-add=NET_RAW",
            "--pids-limit=512",
        ]
    if branch:
        args += ["-e", f"BASE_BRANCH={branch}"]
        args += ["-e", f"REPO_BRANCH={branch}"]  # deprecated alias
    if memory:
        args += [f"--memory={memory}"]
    if cpus:
        args += [f"--cpus={cpus}"]
    if gpus:
        args += [f"--gpus={gpus}"]
    if agent_type:
        args += ["-e", f"AGENT_TYPE={agent_type}"]
    if ci_watch:
        args += ["-e", "CI_WATCH_ENABLED=true"]
    args.append(image)
    return args


# ─── Network helpers ──────────────────────────────────────────────────────────


def get_router_ip(network: str) -> str:
    """Get the router container's IP address on a specific network."""
    r = run_check([
        "docker", "inspect", "sandbox-router",
        "-f", "{{(index .NetworkSettings.Networks \"" + network + "\").IPAddress}}",
    ])
    ip = r.stdout.strip()
    if not ip:
        die(f"Router not connected to network {network}")
    return ip


def get_network_subnet(network: str) -> str:
    """Get the subnet CIDR for a Docker network."""
    r = run_check([
        "docker", "network", "inspect", network,
        "-f", "{{(index .IPAM.Config 0).Subnet}}",
    ])
    return r.stdout.strip()


def inject_route(container: str, router_ip: str) -> None:
    """Inject a default route into a container's network namespace using a throwaway container."""
    run_check([
        "docker", "run", "--rm", "--privileged",
        "--network", f"container:{container}",
        "alpine", "ip", "route", "add", "default", "via", router_ip,
    ])


def apply_firewall_rules(network: str, open_egress: bool) -> None:
    """Apply iptables rules in the router for an agent network."""
    subnet = get_network_subnet(network)
    mode = "open" if open_egress else "locked"
    run_check([
        "docker", "exec", "sandbox-router",
        "/scripts/apply-rules.sh", subnet, mode,
    ])


def remove_firewall_rules(network: str) -> None:
    """Remove iptables rules in the router for an agent network."""
    try:
        subnet = get_network_subnet(network)
        run(["docker", "exec", "sandbox-router",
             "/scripts/remove-rules.sh", subnet], capture_output=True)
    except Exception:
        pass  # Network may already be gone


def ensure_agent_network(project: str, cfg: Config, open_egress: bool = False) -> tuple[str, str]:
    """Create a per-project internal network, connect infrastructure, apply firewall rules.

    Returns (network_name, router_ip).
    """
    network = f"sandbox-net-{project}"
    if not run_quiet(["docker", "network", "inspect", network]):
        run_check(["docker", "network", "create", "--internal", network])
    # Connect infrastructure services (ignore errors if already connected)
    for svc in ["sandbox-gitea", "sandbox-router"]:
        run(["docker", "network", "connect", network, svc], capture_output=True)
    if container_exists("sandbox-webui"):
        run(["docker", "network", "connect", network, "sandbox-webui"],
            capture_output=True)
    router_ip = get_router_ip(network)
    apply_firewall_rules(network, open_egress)
    return network, router_ip


def remove_agent_network(project: str) -> None:
    """Remove firewall rules, disconnect infrastructure, and remove per-project network."""
    network = f"sandbox-net-{project}"
    remove_firewall_rules(network)
    for svc in ["sandbox-gitea", "sandbox-router", "sandbox-webui"]:
        run(["docker", "network", "disconnect", network, svc], capture_output=True)
    run(["docker", "network", "rm", network], capture_output=True)


def _reinject_route(container: str, cfg: Config) -> None:
    """Re-inject the default route and ensure firewall rules after starting a container."""
    # Get the project name from the container's label
    r = subprocess.run(
        ["docker", "inspect", "-f", "{{index .Config.Labels \"sandbox.project\"}}", container],
        capture_output=True, text=True,
    )
    project = r.stdout.strip()
    if not project:
        return

    network = f"sandbox-net-{project}"
    # Reconnect infra services (covers Gitea/router restart via compose)
    for svc in ["sandbox-gitea", "sandbox-router"]:
        run(["docker", "network", "connect", network, svc], capture_output=True)

    try:
        router_ip = get_router_ip(network)
        inject_route(container, router_ip)

        # Re-apply firewall rules (idempotent, covers router restart case)
        r = subprocess.run(
            ["docker", "inspect", "-f", "{{index .Config.Labels \"sandbox.egress\"}}", container],
            capture_output=True, text=True,
        )
        open_egress = r.stdout.strip() == "True"
        apply_firewall_rules(network, open_egress)
        print(f"  Route and firewall rules applied for {project}")
    except Exception as e:
        print(f"  Warning: Failed to inject route for {project}: {e}", file=sys.stderr)


def wire_webui_to_projects() -> None:
    """Connect sandbox-webui to every existing per-project network."""
    if not container_exists("sandbox-webui"):
        return
    r = run(["docker", "network", "ls",
             "--filter", "name=^sandbox-net-",
             "--format", "{{.Name}}"],
            capture_output=True, text=True)
    for net in r.stdout.strip().splitlines():
        if net:
            run(["docker", "network", "connect", net, "sandbox-webui"],
                capture_output=True)


# ─── Port forwarder (socat host-publisher; the CLI `port` command drives it) ──


def port_forwarder_name(project: str) -> str:
    return f"sandbox-port-{project}"


def read_port_state(project: str) -> tuple[str, list[tuple[int, int]]]:
    """Return (bind_ip, [(host_port, cont_port)]) for this project's forwarder.
    Returns ("", []) if no forwarder is running."""
    name = port_forwarder_name(project)
    if not container_exists(name):
        return "", []
    r = run(["docker", "inspect", name, "-f",
             '{{index .Config.Labels "sandbox.bind"}}\t'
             '{{index .Config.Labels "sandbox.mappings"}}'],
            capture_output=True, text=True)
    parts = r.stdout.strip().split("\t")
    bind = parts[0] if parts and parts[0] else "127.0.0.1"
    mappings_str = parts[1] if len(parts) > 1 else ""
    mappings = []
    for m in mappings_str.split(",") if mappings_str else []:
        h, c = m.split(":")
        mappings.append((int(h), int(c)))
    return bind, mappings


def apply_port_state(project: str, bind: str, mappings: list[tuple[int, int]]) -> None:
    """Replace the project's forwarder with one publishing the given mappings.
    If mappings is empty, the forwarder is removed.

    The forwarder is attached to dev-sandbox (so it can publish to the host)
    and to the project's internal net (so it can resolve the agent by name).
    """
    name = port_forwarder_name(project)
    project_net = f"sandbox-net-{project}"
    agent = f"sandbox-agent-{project}"

    if container_exists(name):
        run(["docker", "rm", "-f", name], capture_output=True)

    if not mappings:
        return

    socat_chain = " ".join(
        f"socat TCP-LISTEN:{h},fork,reuseaddr TCP:{agent}:{c} &"
        for h, c in mappings
    ) + " wait"

    args = ["docker", "run", "-d", "--rm",
            "--name", name,
            "--network", "dev-sandbox",
            "--label", f"sandbox.project={project}",
            "--label", "sandbox.port-forwarder=true",
            "--label", f"sandbox.bind={bind}",
            "--label", "sandbox.mappings=" + ",".join(f"{h}:{c}" for h, c in mappings)]
    for h, _ in mappings:
        args += ["-p", f"{bind}:{h}:{h}"]
    args += ["--entrypoint", "sh", PORT_FORWARDER_IMAGE, "-c", socat_chain]
    run_check(args)
    run_check(["docker", "network", "connect", project_net, name])


def remove_port_forwarder(project: str) -> None:
    """Tear down the project's port forwarder (used by destroy)."""
    name = port_forwarder_name(project)
    if container_exists(name):
        run(["docker", "rm", "-f", name], capture_output=True)


# ─── Workspace volume helpers (shared by create / recreate / destroy / unsetup)


def create_workspace_volume(cfg: Config, project: str, volume_name: str) -> None:
    """Create the project volume (bind-mounted under PROJECTS_DIR when set).

    Unconditional: run_check fails loudly if the volume already exists —
    recreate relies on that to surface a `docker volume rm` that silently
    failed (silent reuse would break its fresh-clone promise). create() guards
    with a volume-inspect at the call site.
    """
    if cfg.projects_dir:
        workspace_dir = Path(cfg.projects_dir) / project
        workspace_dir.mkdir(parents=True, exist_ok=True)
        run_check(["docker", "volume", "create", "--driver", "local",
                   "--opt", "type=none", "--opt", f"device={workspace_dir}",
                   "--opt", "o=bind", volume_name])
    else:
        run_check(["docker", "volume", "create", volume_name])


def copy_container_files(volume_name: str, agent_type: str) -> None:
    """Copy container/ files to agent home and fix ownership for bind mounts.

    Two-layer copy: universal files from container/, then agent-specific overlay
    from container/<agent>/ (if an agent was specified).
    Uses host GID so the invoking user gets group rw access to container_volumes/.
    """
    host_gid = os.getgid()
    container_src = SCRIPT_DIR / "container"
    agent_src = container_src / agent_type if agent_type else None
    docker_copy_args = ["docker", "run", "--rm", "-v", f"{volume_name}:/home/agent"]
    copy_cmds = []
    if container_src.is_dir():
        docker_copy_args += ["-v", f"{container_src}:/src:ro"]
        copy_cmds.append("find /src -maxdepth 1 -type f -exec cp {} /home/agent/ \\;")
    if agent_src and agent_src.is_dir():
        docker_copy_args += ["-v", f"{agent_src}:/agent-src:ro"]
        copy_cmds.append("cp /agent-src/* /home/agent/")
    copy_cmds.append(f"chmod +x /home/agent/*.sh 2>/dev/null; chown -R 1000:{host_gid} /home/agent && chmod 2770 /home/agent")
    docker_copy_args += ["alpine", "sh", "-c", " && ".join(copy_cmds)]
    run_check(docker_copy_args)


def remove_workspace_dir(cfg: Config, project: str) -> None:
    """Remove the project's workspace directory. Silent — call sites own the
    is_dir() guard and any progress print (destroy/recreate print, unsetup
    doesn't)."""
    workspace_dir = Path(cfg.projects_dir) / project
    try:
        shutil.rmtree(workspace_dir)
    except PermissionError:
        # Files created inside containers are owned by root
        run(["docker", "run", "--rm", "-v", f"{workspace_dir.resolve()}:/mnt/ws",
             "alpine", "rm", "-rf", "/mnt/ws"], capture_output=True)
        if workspace_dir.is_dir():
            workspace_dir.rmdir()


def _agent_ssh_info(project: str) -> tuple[str, str] | None:
    """Return (ssh_port, ssh_password) for an existing agent, or None."""
    container = f"sandbox-agent-{project}"
    if not container_exists(container):
        return None
    r = run(["docker", "inspect", container, "-f",
             '{{range $p, $binds := .HostConfig.PortBindings}}'
             '{{range $binds}}{{.HostPort}}{{end}}{{end}}'],
            capture_output=True, text=True)
    if r.returncode != 0 or not r.stdout.strip():
        return None
    ssh_port = r.stdout.strip()
    r = run(["docker", "inspect", container, "-f",
             '{{range .Config.Env}}{{println .}}{{end}}'],
            capture_output=True, text=True)
    if r.returncode != 0:
        return None
    ssh_pass = None
    for line in r.stdout.splitlines():
        if line.startswith("SSH_PASSWORD="):
            ssh_pass = line.split("=", 1)[1]
            break
    if not ssh_pass:
        return None
    return ssh_port, ssh_pass


# ─── Request dataclasses (the validation choke points) ────────────────────────

# Project names feed container/network/volume/Gitea-user names. ASCII
# letters/digits, then also '.', '_', '-'; must start with a letter or digit
# (leading '-' reads as a flag downstream; unicode is a Docker footgun). Dot is
# allowed because names derive from GitHub repos ("next.js" must keep working).
_PROJECT_NAME_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]*\Z")

# docker --memory format: positive number + optional b/k/m/g unit suffix.
_MEMORY_RE = re.compile(r"\A\d+(\.\d+)?[bkmgBKMG]?\Z")

# Characters git forbids in ref names (plus whitespace/control chars, checked
# separately). A branch value reaches `docker run -e` and in-container git.
_BRANCH_FORBIDDEN = set("~^:?*[\\")


def _require_project(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise ValidationError("project name is required")
    if not _PROJECT_NAME_RE.match(value):
        raise ValidationError(
            f"invalid project name {value!r}: must start with a letter or digit "
            "and contain only letters, digits, '.', '_' or '-'")
    return value


def _validate_branch(branch: str) -> str:
    if not branch:
        return ""
    if branch.startswith("-"):
        raise ValidationError(f"invalid branch name {branch!r} (leading '-')")
    for c in branch:
        if c.isspace() or ord(c) < 32 or c in _BRANCH_FORBIDDEN:
            raise ValidationError(
                f"invalid branch name {branch!r}: whitespace, control characters "
                "and '~^:?*[\\' are not allowed")
    return branch


@dataclass(frozen=True)
class CreateRequest:
    github_url: str
    project: str          # derived from github_url in from_kwargs
    branch: str = ""
    egress: str = ""      # "" ⇒ .env default; else "locked" | "open"
    memory: str = ""      # "" ⇒ cfg.default_memory
    cpus: str = ""
    gpus: str = ""        # host device attachment — CLI-only, never broker-relayed
    profile: str = ""     # "" ⇒ cfg.default_profile, resolved in the verb
    ssh_port: int = 0     # 0 ⇒ auto; host port — CLI-only, never broker-relayed
    agent: str = ""
    docker: bool = False

    @classmethod
    def from_kwargs(cls, **kw: Any) -> "CreateRequest":
        github_url = str(kw.get("github_url") or "").strip()
        if not github_url:
            raise ValidationError("github-url is required")
        split = urlsplit(github_url)
        if split.scheme not in ("http", "https") or not split.netloc:
            raise ValidationError(
                f"github-url must be an http(s):// URL with a host (got {github_url!r})")
        project = parse_project_name(github_url)
        if not _PROJECT_NAME_RE.match(project):
            raise ValidationError(
                f"repo name {project!r} (derived from {github_url}) must start with a "
                "letter or digit and contain only letters, digits, '.', '_' or '-'")
        branch = _validate_branch(str(kw.get("branch") or ""))
        egress = str(kw.get("egress") or "")
        if egress not in ("", "locked", "open"):
            raise ValidationError(f"egress must be 'locked' or 'open' (got {egress!r})")
        memory = str(kw.get("memory") or "")
        if memory and not _MEMORY_RE.match(memory):
            raise ValidationError(
                f"invalid --memory value {memory!r} (expected e.g. 512m, 8g)")
        cpus = str(kw.get("cpus") or "")
        if cpus:
            try:
                cpus_val = float(cpus)
            except ValueError:
                raise ValidationError(f"invalid --cpus value {cpus!r} (expected a number)")
            if cpus_val <= 0:
                raise ValidationError(f"--cpus must be positive (got {cpus})")
        gpus = str(kw.get("gpus") or "")
        profile = str(kw.get("profile") or "")
        if profile:
            resolve_profile_image(profile)  # raises ValidationError on unknown profile
        ssh_port = kw.get("ssh_port") or 0
        if isinstance(ssh_port, bool) or not isinstance(ssh_port, int):
            raise ValidationError("ssh-port must be an integer")
        if ssh_port and not 1 <= ssh_port <= 65535:
            raise ValidationError(f"ssh-port must be 1-65535 (got {ssh_port})")
        agent = str(kw.get("agent") or "")
        if agent:
            if not (SCRIPT_DIR / "container" / agent).is_dir():
                raise ValidationError(
                    f"Unknown agent '{agent}'. Available: {', '.join(list_agents())}")
            if agent not in INSTALLERS:
                raise ValidationError(f"No installer for agent '{agent}'.")
        return cls(github_url=github_url, project=project, branch=branch,
                   egress=egress, memory=memory, cpus=cpus, gpus=gpus,
                   profile=profile, ssh_port=ssh_port, agent=agent,
                   docker=bool(kw.get("docker", False)))


@dataclass(frozen=True)
class DestroyRequest:
    project: str
    # Confirmation is a front-end concern (terminal prompt; the broker's
    # step-up proof). The verb just destroys — no prompting in the core.

    @classmethod
    def from_kwargs(cls, **kw: Any) -> "DestroyRequest":
        return cls(project=_require_project(kw.get("project")))


@dataclass(frozen=True)
class StartRequest:
    project: str

    @classmethod
    def from_kwargs(cls, **kw: Any) -> "StartRequest":
        return cls(project=_require_project(kw.get("project")))


@dataclass(frozen=True)
class StopRequest:
    project: str

    @classmethod
    def from_kwargs(cls, **kw: Any) -> "StopRequest":
        return cls(project=_require_project(kw.get("project")))


@dataclass(frozen=True)
class SyncRequest:
    project: str

    @classmethod
    def from_kwargs(cls, **kw: Any) -> "SyncRequest":
        return cls(project=_require_project(kw.get("project")))


@dataclass(frozen=True)
class AttachRequest:
    project: str

    @classmethod
    def from_kwargs(cls, **kw: Any) -> "AttachRequest":
        return cls(project=_require_project(kw.get("project")))


def _require_webport(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError("port must be an integer")
    if not WEBPORT_PORT_MIN <= value <= WEBPORT_PORT_MAX:
        raise ValidationError(
            f"port must be {WEBPORT_PORT_MIN}-{WEBPORT_PORT_MAX} (got {value})")
    return value


@dataclass(frozen=True)
class WebportAddRequest:
    project: str
    port: int
    label: str

    @classmethod
    def from_kwargs(cls, **kw: Any) -> "WebportAddRequest":
        label = kw.get("label")
        if not isinstance(label, str) or not _WEBPORT_LABEL_RE.match(label):
            raise ValidationError(
                "label must be 1-32 characters: letters, digits, space, '.', '_' "
                "or '-', starting with a letter or digit")
        return cls(project=_require_project(kw.get("project")),
                   port=_require_webport(kw.get("port")), label=label)


@dataclass(frozen=True)
class WebportRemoveRequest:
    project: str
    port: int

    @classmethod
    def from_kwargs(cls, **kw: Any) -> "WebportRemoveRequest":
        return cls(project=_require_project(kw.get("project")),
                   port=_require_webport(kw.get("port")))


@dataclass(frozen=True)
class WebportListRequest:
    project: str

    @classmethod
    def from_kwargs(cls, **kw: Any) -> "WebportListRequest":
        return cls(project=_require_project(kw.get("project")))


# ─── Result dataclasses ───────────────────────────────────────────────────────


@dataclass
class CreateResult:
    project: str
    container: str
    gitea_user: str
    image: str
    profile: str
    agent: str
    ssh_port: int
    ssh_password: str     # in-memory only; only CreateResult/AttachInfo carry it
    egress: str           # "open" | "locked" (resolved, not the request field)
    base_branch: str
    docker: bool


@dataclass
class DestroyResult:
    project: str
    removed_container: bool
    removed_volume: bool
    removed_workspace: bool
    removed_gitea_user: bool
    removed_mirror: bool


@dataclass
class ActionResult:
    project: str
    container: str
    action: str           # "start" | "stop"
    ok: bool


@dataclass
class SyncResult:
    project: str
    mirrored: bool
    pulled: bool


@dataclass
class ProjectSummary:
    """Broker sidebar row. Never carries credentials."""
    project: str
    state: str
    ssh_port: str | None  # published host port, or None


@dataclass
class InfraStatus:
    name: str
    state: str


@dataclass
class AgentStatus:
    project: str
    state: str
    ssh_port: str
    port_bind: str
    port_mappings: list[tuple[int, int]]


@dataclass
class StatusResult:
    infra: list[InfraStatus]
    ci_test_lines: list[str]   # raw tab-separated `docker ps` rows; formatter splits
    agents: list[AgentStatus]
    projects_dir: str
    project_dirs: list[str]


@dataclass
class AttachInfo:
    """SSH coordinates for the webui to reach a RUNNING agent over its
    per-project network: container DNS + the internal sshd port — never the
    published host port. The password rides back in-memory; never printed."""
    project: str
    host: str             # sandbox-agent-<project> (container DNS)
    port: int             # internal sshd port (22)
    username: str
    password: str


@dataclass
class Webport:
    port: int
    label: str


@dataclass
class WebportAddResult:
    project: str
    port: int
    label: str


@dataclass
class WebportRemoveResult:
    project: str
    port: int


# ─── Progress (webui checklist milestones) ────────────────────────────────────


class _NullProgress:
    """No-op milestone sink — the default when a verb is driven from the CLI.
    The broker passes a real sink (duck-typed: anything with a step(key, msg)
    method) for webui-fired ops; the core never imports the broker, so the
    contract is structural, not a shared type."""

    def step(self, key: str, msg: str = "") -> None:
        pass


_NULL_PROGRESS = _NullProgress()

# Milestone keys per verb, in emission order (= execution order). LOCKSTEP:
# the webui's OP_CHECKLISTS (Stage 3) must match these exactly. The terminal
# "done"/"fail" row is dispatcher-owned (Stage 2), not a verb milestone.
PROGRESS_KEYS = {
    "create": ["validate", "gitea", "build-image", "network", "create-container",
               "route", "agent-install", "ready"],
    "destroy": ["validate", "forwarder", "remove-container", "cleanup", "gitea"],
    "start": ["validate", "start", "route"],
    "stop": ["validate", "stop"],
    "sync": ["validate", "mirror", "pull"],
}


# ─── Lifecycle verbs ──────────────────────────────────────────────────────────


def create(req: CreateRequest, cfg: Config | None = None, progress=None) -> CreateResult:
    """Create a project sandbox. ``req`` is already validated.

    Chatty step prints stay here on purpose (the broker tees verb stdout into
    its host-only full log); the CLI shim prints the final summary from the
    returned CreateResult. Gitea steps that can surface credentials in error
    text are wrapped into HarnessError with a scrubbed client detail.
    """
    if cfg is None:
        cfg = load_config()
    progress = progress or _NULL_PROGRESS

    progress.step("validate")
    if req.docker and not sysbox_available():
        die("--docker requires Sysbox runtime. See: https://github.com/nestybox/sysbox#installation")
    project = req.project
    container_name = f"sandbox-agent-{project}"
    gitea_user = f"agent-{project}"
    volume_name = f"sandbox-{project}"

    print(f"=== Creating sandbox: {project} ===")

    progress.step("gitea")
    try:
        # 1. Create Gitea mirror
        print(f"Mirroring {req.github_url} to Gitea...")
        if gitea_api_ok(cfg, "GET", f"/repos/sandbox-admin/{project}"):
            print("Mirror already exists, triggering sync...")
            gitea_api_ok(cfg, "POST", f"/repos/sandbox-admin/{project}/mirror-sync")
        else:
            migrate_payload = {
                "clone_addr": req.github_url,
                "repo_name": project,
                "repo_owner": "sandbox-admin",
                "mirror": True,
                "service": "github",
            }
            if cfg.github_pat:
                migrate_payload["auth_token"] = cfg.github_pat
            gitea_api(cfg, "POST", "/repos/migrate", migrate_payload)
            print("Mirror created. Waiting for initial sync...")
            time.sleep(5)
        gitea_api_ok(cfg, "PATCH", f"/repos/sandbox-admin/{project}",
                     {"description": f"Read-only mirror of {req.github_url}"})

        # 2. Create per-project Gitea user
        print(f"Setting up Gitea user: {gitea_user}...")
        user_pass = gen_password()

        if not gitea_api_ok(cfg, "GET", f"/users/{gitea_user}"):
            gitea_api(cfg, "POST", "/admin/users", {
                "username": gitea_user,
                "password": user_pass,
                "email": f"{gitea_user}@sandbox.local",
                "must_change_password": False,
                "visibility": "public",
            })
        else:
            # User exists (re-run after partial failure) — reset password so we can auth
            gitea_api(cfg, "PATCH", f"/admin/users/{gitea_user}", {
                "login_name": gitea_user,
                "source_id": 0,
                "password": user_pass,
                "must_change_password": False,
            })

        # Fork mirror to agent user (fork as the user so it lands in their namespace)
        if not gitea_api_ok(cfg, "GET", f"/repos/{gitea_user}/{project}"):
            fork_url = f"http://localhost:{cfg.gitea_port}/api/v1/repos/sandbox-admin/{project}/forks"
            try:
                http_basic_auth_request(fork_url, gitea_user, user_pass, method="POST", body={})
            except (HTTPError, URLError) as e:
                die(f"Failed to fork repo to {gitea_user}: {e}")
            time.sleep(2)

        # Enable repo features and grant admin access to the agent fork (for Gitea webui)
        repo_features = {
            "has_issues": True,
            "has_wiki": True,
            "has_pull_requests": True,
            "has_projects": True,
        }
        gitea_api_ok(cfg, "PATCH", f"/repos/{gitea_user}/{project}",
                     {"description": "Agent workspace", **repo_features})
        gitea_api_ok(cfg, "PATCH", f"/repos/sandbox-admin/{project}",
                     {**repo_features, "has_issues": False})
        gitea_api_ok(cfg, "PUT", f"/repos/{gitea_user}/{project}/collaborators/sandbox-admin",
                     {"permission": "admin"})
        # Grant CI watch write access if configured (so sandbox-ci can post and attach to comments)
        if cfg.ci_watch_gitea_token:
            gitea_api_ok(cfg, "PUT", f"/repos/{gitea_user}/{project}/collaborators/sandbox-ci",
                         {"permission": "write"})
        gitea_api_ok(cfg, "PUT", f"/repos/{gitea_user}/{project}/subscription")

        # Determine base branch: --branch flag → Gitea mirror's default_branch
        base_branch = req.branch
        if not base_branch:
            repo_info = gitea_api_or(cfg, "GET", f"/repos/sandbox-admin/{project}", {})
            if isinstance(repo_info, dict):
                base_branch = repo_info.get("default_branch", "")
            if base_branch:
                print(f"Base branch: {base_branch} (from repo default)")

        # 3. Generate fresh Gitea token
        print(f"Generating Gitea token for {gitea_user}...")
        agent_token = generate_gitea_token(cfg, gitea_user, user_pass)
    except (RuntimeError, HTTPError, URLError) as e:
        raise HarnessError("gitea setup failed", _scrub_secrets(str(e), cfg)) from e

    # 4. Build agent image if needed
    progress.step("build-image")
    profile = req.profile or cfg.default_profile
    if not profile:
        available = sorted(
            p.name.removeprefix("Dockerfile.")
            for p in (SCRIPT_DIR / "agent").glob("Dockerfile.*")
            if not p.name.endswith(".sh")
        )
        die(f"--profile is required. Available: {', '.join(available)}")
    image, dockerfile = resolve_profile_image(profile)
    if not run_quiet(["docker", "image", "inspect", image]):
        print(f"Building agent image: {image} (profile: {profile})...")
        run_check(["docker", "build", "-t", image, "-f", str(dockerfile), str(SCRIPT_DIR / "agent")])

    # 6. Create Docker volume (guarded here; the helper itself is unconditional)
    if not run_quiet(["docker", "volume", "inspect", volume_name]):
        create_workspace_volume(cfg, project, volume_name)

    copy_container_files(volume_name, req.agent)

    # 7. Create per-project network and connect infrastructure
    progress.step("network")
    print("Setting up agent network...")
    open_egress = (req.egress == "open") if req.egress else cfg.default_open_egress
    agent_network, router_ip = ensure_agent_network(project, cfg, open_egress)

    # 8. Remove existing container
    progress.step("create-container")
    if container_exists(container_name):
        print("Removing existing container...")
        run(["docker", "rm", "-f", container_name], capture_output=True)

    # 9. Start agent container
    print("Starting agent container...")

    ssh_port = req.ssh_port or find_free_port(2222)
    ssh_pass = gen_password()
    memory = req.memory or cfg.default_memory

    docker_args = build_agent_docker_args(
        container_name=container_name, project=project, network=agent_network,
        volume_name=volume_name, ssh_port=ssh_port, agent_token=agent_token,
        gitea_user=gitea_user, ssh_pass=ssh_pass,
        dns_servers=cfg.dns_servers, memory=memory, open_egress=open_egress, image=image,
        branch=base_branch or "", cpus=req.cpus or "",
        gpus=req.gpus or profile_default_gpus(profile), agent_type=req.agent,
        docker=req.docker, ci_watch=cfg.ci_watch_enabled,
    )
    run_check(["docker", *docker_args])

    # 10. Inject default route through the router
    progress.step("route")
    print("Injecting network route...")
    inject_route(container_name, router_ip)

    # 11./12. Install agent CLI / Docker-in-Docker (need network, so after route),
    # then start byobu (after all installs so the shell inherits the final env)
    progress.step("agent-install")
    if req.agent:
        install_agent(req.agent, container_name)
    if req.docker:
        install_docker_dind(container_name)
    start_byobu_session(container_name)

    progress.step("ready")
    return CreateResult(
        project=project, container=container_name, gitea_user=gitea_user,
        image=image, profile=profile, agent=req.agent,
        ssh_port=ssh_port, ssh_password=ssh_pass,
        egress="open" if open_egress else "locked",
        base_branch=base_branch or "", docker=req.docker,
    )


def destroy(req: DestroyRequest, cfg: Config | None = None, progress=None) -> DestroyResult:
    """Destroy a project sandbox. Confirmation happens in the front-end
    (CLI prompt / broker step-up), never here."""
    if cfg is None:
        cfg = load_config()
    progress = progress or _NULL_PROGRESS

    progress.step("validate")
    project = req.project
    container_name = f"sandbox-agent-{project}"
    gitea_user = f"agent-{project}"
    volume_name = f"sandbox-{project}"

    print(f"\nDestroying sandbox: {project}...")

    progress.step("forwarder")
    remove_port_forwarder(project)

    progress.step("remove-container")
    removed_container = container_exists(container_name)
    if removed_container:
        print("Removing container...")
        run(["docker", "rm", "-f", container_name], capture_output=True)

    removed_volume = run_quiet(["docker", "volume", "inspect", volume_name])
    if removed_volume:
        print("Removing Docker volume...")
        run(["docker", "volume", "rm", volume_name], capture_output=True)

    removed_workspace = False
    if cfg.projects_dir:
        workspace_dir = Path(cfg.projects_dir) / project
        if workspace_dir.is_dir():
            print("Removing workspace directory...")
            remove_workspace_dir(cfg, project)
            removed_workspace = True

    # Remove per-project network
    progress.step("cleanup")
    print("Removing agent network...")
    remove_agent_network(project)

    progress.step("gitea")
    removed_gitea_user = gitea_api_ok(cfg, "GET", f"/users/{gitea_user}")
    if removed_gitea_user:
        print(f"Removing Gitea user {gitea_user}...")
        gitea_api_ok(cfg, "DELETE", f"/admin/users/{gitea_user}?purge=true")

    removed_mirror = gitea_api_ok(cfg, "GET", f"/repos/sandbox-admin/{project}")
    if removed_mirror:
        print(f"Removing Gitea mirror (sandbox-admin/{project})...")
        gitea_api_ok(cfg, "DELETE", f"/repos/sandbox-admin/{project}")

    return DestroyResult(project=project, removed_container=removed_container,
                         removed_volume=removed_volume,
                         removed_workspace=removed_workspace,
                         removed_gitea_user=removed_gitea_user,
                         removed_mirror=removed_mirror)


def start(req: StartRequest, cfg: Config | None = None, progress=None) -> ActionResult:
    """Start the agent container, re-inject its route, restart byobu."""
    if cfg is None:
        cfg = load_config()
    progress = progress or _NULL_PROGRESS

    progress.step("validate")
    container = f"sandbox-agent-{req.project}"
    if not container_exists(container):
        die(f"Container {container} not found.")

    progress.step("start")
    r = run(["docker", "start", container])  # uncaptured: docker's echo is the output

    progress.step("route")
    _reinject_route(container, cfg)
    start_byobu_session(container)
    return ActionResult(project=req.project, container=container,
                        action="start", ok=r.returncode == 0)


def stop(req: StopRequest, cfg: Config | None = None, progress=None) -> ActionResult:
    """Stop the agent container. ``cfg`` is accepted for signature uniformity
    but never loaded or used — `sandbox stop` works without .env."""
    progress = progress or _NULL_PROGRESS

    progress.step("validate")
    container = f"sandbox-agent-{req.project}"
    if not container_exists(container):
        die(f"Container {container} not found.")

    progress.step("stop")
    r = run(["docker", "stop", container])  # uncaptured: docker's echo is the output
    return ActionResult(project=req.project, container=container,
                        action="stop", ok=r.returncode == 0)


def sync(req: SyncRequest, cfg: Config | None = None, progress=None) -> SyncResult:
    """Trigger the Gitea mirror sync and pull in the running container."""
    if cfg is None:
        cfg = load_config()
    progress = progress or _NULL_PROGRESS

    progress.step("validate")
    project = req.project

    progress.step("mirror")
    print(f"Triggering mirror sync for {project}...")
    mirrored = gitea_api_ok(cfg, "POST", f"/repos/sandbox-admin/{project}/mirror-sync")

    progress.step("pull")
    container = f"sandbox-agent-{project}"
    pulled = False
    if container_running(container):
        print("Pulling latest in container...")
        run(["docker", "exec", container, "bash", "-c",
             f"cd /home/agent/{project} && git pull --ff-only"], capture_output=True)
        pulled = True

    return SyncResult(project=project, mirrored=mirrored, pulled=pulled)


def status(cfg: Config | None = None) -> StatusResult:
    """Snapshot infra/agents/CI-test state. Read-only, prints nothing —
    the CLI table (and later the broker reply) is a formatter over this."""
    if cfg is None:
        cfg = load_config()

    infra = []
    for svc in ["sandbox-gitea", "sandbox-router"]:
        r = subprocess.run(["docker", "inspect", "-f", "{{.State.Status}}", svc],
                           capture_output=True, text=True)
        infra.append(InfraStatus(
            name=svc, state=r.stdout.strip() if r.returncode == 0 else "not found"))

    r = subprocess.run(
        ["docker", "ps", "--filter", "label=sandbox.ci-test=true",
         "--format", "{{.Names}}\t{{.Status}}\t{{.Label \"sandbox.ci-pr\"}}"],
        capture_output=True, text=True)
    ci_test_lines = [line for line in r.stdout.strip().splitlines() if line]

    agents = []
    for name in get_agent_containers():
        project_name = name.removeprefix("sandbox-agent-")
        r = subprocess.run(
            ["docker", "inspect", "-f",
             "{{.State.Status}}\t{{range $p, $binds := .HostConfig.PortBindings}}"
             "{{range $binds}}{{.HostPort}}{{end}}{{end}}", name],
            capture_output=True, text=True,
        )
        parts = r.stdout.strip().split("\t") if r.returncode == 0 else ["?", ""]
        state = parts[0] if parts else "?"
        ssh_port = parts[1] if len(parts) > 1 and parts[1] else "-"
        b, ms = read_port_state(project_name)
        agents.append(AgentStatus(project=project_name, state=state,
                                  ssh_port=ssh_port, port_bind=b, port_mappings=ms))

    project_dirs = []
    if cfg.projects_dir:
        projects_path = Path(cfg.projects_dir)
        if projects_path.is_dir():
            project_dirs = [d.name for d in sorted(projects_path.iterdir()) if d.is_dir()]

    return StatusResult(infra=infra, ci_test_lines=ci_test_lines, agents=agents,
                        projects_dir=cfg.projects_dir, project_dirs=project_dirs)


def list_projects(cfg: Config | None = None) -> list[ProjectSummary]:
    """One row per agent container — the broker sidebar read. No credentials."""
    summaries = []
    for name in get_agent_containers():
        project = name.removeprefix("sandbox-agent-")
        r = subprocess.run(
            ["docker", "inspect", "-f",
             "{{.State.Status}}\t{{range $p, $binds := .HostConfig.PortBindings}}"
             "{{range $binds}}{{.HostPort}}{{end}}{{end}}", name],
            capture_output=True, text=True,
        )
        parts = r.stdout.strip().split("\t") if r.returncode == 0 else ["?", ""]
        state = parts[0] if parts else "?"
        ssh_port = parts[1] if len(parts) > 1 and parts[1] else ""
        summaries.append(ProjectSummary(project=project, state=state,
                                        ssh_port=ssh_port or None))
    return summaries


def attach_info(req: AttachRequest, cfg: Config | None = None) -> AttachInfo:
    """JIT SSH coordinates for a running agent, over the per-project network
    (container DNS, internal port 22) — never the published host port."""
    container = f"sandbox-agent-{req.project}"
    if not container_exists(container):
        die(f"Container {container} not found.")
    if not container_running(container):
        die(f"Container {container} is not running. Run: sandbox start {req.project}")
    r = run(["docker", "inspect", container, "-f",
             '{{range .Config.Env}}{{println .}}{{end}}'],
            capture_output=True, text=True)
    password = ""
    if r.returncode == 0:
        for line in r.stdout.splitlines():
            if line.startswith("SSH_PASSWORD="):
                password = line.split("=", 1)[1]
                break
    if not password:
        die(f"Container {container} has no SSH_PASSWORD in its environment.")
    return AttachInfo(project=req.project, host=container, port=22,
                      username="agent", password=password)


# ─── Webport registry verbs (broker-owned port-tab registry) ─────────────────


def _read_webports() -> dict:
    """Read the registry ({"<project>": [{"port": N, "label": "..."}]})."""
    if not WEBPORTS_FILE.exists():
        return {}
    try:
        data = json.loads(WEBPORTS_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_webports(data: dict) -> None:
    """Atomically replace the registry (tmp + rename). Host-only 0700 dirs."""
    for d in (BROKER_DIR, BROKER_RUN_DIR, WEBPORTS_FILE.parent):
        d.mkdir(mode=0o700, exist_ok=True)
    tmp = WEBPORTS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    os.replace(tmp, WEBPORTS_FILE)


def webport_add(req: WebportAddRequest, cfg: Config | None = None) -> WebportAddResult:
    container = f"sandbox-agent-{req.project}"
    if not container_exists(container):
        raise ValidationError(
            f"project '{req.project}' not found (no container {container})")
    data = _read_webports()
    entries = list(data.get(req.project) or [])
    if any(e.get("port") == req.port for e in entries):
        raise ValidationError(f"port {req.port} already registered for {req.project}")
    if len(entries) >= WEBPORTS_PER_PROJECT_CAP:
        raise ValidationError(
            f"{req.project} already has {WEBPORTS_PER_PROJECT_CAP} port tabs "
            "(the cap — serve one index page instead)")
    entries.append({"port": req.port, "label": req.label})
    data[req.project] = entries
    _write_webports(data)
    return WebportAddResult(project=req.project, port=req.port, label=req.label)


def webport_remove(req: WebportRemoveRequest, cfg: Config | None = None) -> WebportRemoveResult:
    # No container check: removal must keep working after the container is gone.
    data = _read_webports()
    entries = list(data.get(req.project) or [])
    remaining = [e for e in entries if e.get("port") != req.port]
    if len(remaining) == len(entries):
        raise ValidationError(f"port {req.port} not registered for {req.project}")
    if remaining:
        data[req.project] = remaining
    else:
        data.pop(req.project, None)
    _write_webports(data)
    return WebportRemoveResult(project=req.project, port=req.port)


def webport_list(req: WebportListRequest, cfg: Config | None = None) -> list[Webport]:
    return [Webport(port=e["port"], label=e.get("label", ""))
            for e in _read_webports().get(req.project) or []
            if isinstance(e, dict) and isinstance(e.get("port"), int)]
