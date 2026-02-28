#!/usr/bin/env python3
"""sandbox.py — Main CLI for the LLM Agent Sandbox."""

import argparse
import base64
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

SCRIPT_DIR = Path(__file__).resolve().parent
GITEA_INTERNAL_URL = "http://sandbox-gitea:3000"

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

    # Resolve relative projects_dir to absolute
    if cfg.projects_dir:
        cfg.projects_dir = str((SCRIPT_DIR / cfg.projects_dir).resolve())

    return cfg


# ─── Helpers ──────────────────────────────────────────────────────────────────


def die(msg: str) -> None:
    print(f"Error: {msg}", file=sys.stderr)
    sys.exit(1)


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


def resolve_profile_image(profile: str) -> tuple[str, Path]:
    """Return (image_tag, dockerfile_path) for a given profile name."""
    dockerfile = SCRIPT_DIR / "agent" / f"Dockerfile.{profile}"
    if not dockerfile.exists():
        available = sorted(
            p.name.removeprefix("Dockerfile.")
            for p in (SCRIPT_DIR / "agent").glob("Dockerfile.*")
            if not p.name.endswith(".sh")
        )
        die(f"Unknown profile '{profile}'. Available: {', '.join(available)}")
    image_tag = f"sandbox-agent-{profile}:latest"
    return image_tag, dockerfile


# Profiles that need GPU passthrough by default.
_GPU_PROFILES = {"cuda"}


def profile_default_gpus(profile: str) -> str:
    """Return default --gpus value for a profile (empty string means none)."""
    return "all" if profile in _GPU_PROFILES else ""


def get_agent_containers() -> list[str]:
    r = subprocess.run(
        ["docker", "ps", "-a", "--filter", "name=^sandbox-agent-", "--format", "{{.Names}}"],
        capture_output=True, text=True,
    )
    return [n for n in r.stdout.strip().splitlines() if n]


def container_exists(name: str) -> bool:
    return run_quiet(["docker", "ps", "-a", "--format", "{{.Names}}", "--filter", f"name=^{name}$"])


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


def docker_compose(*args: str) -> None:
    run_check([
        "docker", "compose",
        "-f", str(SCRIPT_DIR / "docker-compose.yml"),
        "--env-file", str(SCRIPT_DIR / ".env"),
        *args,
    ])


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
    claude_yolo: bool = False,
    docker: bool = False,
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
    if claude_yolo:
        args += ["-e", "CLAUDE_YOLO=true"]
    args.append(image)
    return args


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
        "scopes": ["all"],
    })
    token = resp.get("sha1") or resp.get("token") or ""
    if not token:
        die(f"Failed to generate Gitea token for {gitea_user}: {resp}")
    return token


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
    router_ip = get_router_ip(network)
    apply_firewall_rules(network, open_egress)
    return network, router_ip


def remove_agent_network(project: str) -> None:
    """Remove firewall rules, disconnect infrastructure, and remove per-project network."""
    network = f"sandbox-net-{project}"
    remove_firewall_rules(network)
    for svc in ["sandbox-gitea", "sandbox-router"]:
        run(["docker", "network", "disconnect", network, svc], capture_output=True)
    run(["docker", "network", "rm", network], capture_output=True)


def update_env_key(key: str, value: str) -> None:
    """Update or append a key=value pair in .env."""
    env_file = SCRIPT_DIR / ".env"
    if not env_file.exists():
        env_file.write_text(f"{key}={value}\n")
        return
    lines = env_file.read_text().splitlines()
    found = False
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith(f"{key}=") or stripped.startswith(f"# {key}="):
            lines[i] = f"{key}={value}"
            found = True
            break
    if not found:
        lines.append(f"{key}={value}")
    env_file.write_text("\n".join(lines) + "\n")


def ensure_env_file() -> None:
    """Copy .env.example to .env if .env does not exist."""
    env_file = SCRIPT_DIR / ".env"
    env_example = SCRIPT_DIR / ".env.example"
    if env_file.exists():
        return
    if not env_example.exists():
        die(".env.example not found. Cannot create .env.")
    shutil.copy2(env_example, env_file)
    print("Created .env from .env.example.")


def read_env_value(key: str) -> str:
    """Read the value of a key from .env (ignoring commented lines)."""
    env_file = SCRIPT_DIR / ".env"
    for line in env_file.read_text().splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or not stripped:
            continue
        if "=" in stripped:
            k, _, v = stripped.partition("=")
            if k.strip() == key:
                return v.strip()
    return ""


def prompt_env_settings() -> None:
    """Prompt for key .env settings that are not yet configured."""

    # ── GITEA_PORT ──
    current_port = read_env_value("GITEA_PORT")
    default_port = current_port or "3000"
    port = input(f"Gitea port [{default_port}]: ").strip()
    if port and port != default_port:
        update_env_key("GITEA_PORT", port)
    elif not current_port:
        update_env_key("GITEA_PORT", default_port)

    # ── PROJECTS_DIR ──
    current_dir = read_env_value("PROJECTS_DIR")
    default_dir = current_dir or "./container_volumes/"
    proj_dir = input(f"Projects directory [{default_dir}]: ").strip()
    if proj_dir and proj_dir != default_dir:
        update_env_key("PROJECTS_DIR", proj_dir)
    elif not current_dir:
        update_env_key("PROJECTS_DIR", default_dir)

    # ── GITHUB_PAT ──
    if not read_env_value("GITHUB_PAT"):
        print("\nGITHUB_PAT is not set.")
        print("A GitHub Personal Access Token allows read-only mirroring of private repos.")
        print("Check the README for how to create one.")
        answer = input("Would you like to set it now? [y/N] ").strip().lower()
        if answer in ("y", "yes"):
            pat = input("Enter your GitHub PAT: ").strip()
            if pat:
                update_env_key("GITHUB_PAT", pat)
                print("GITHUB_PAT saved to .env.")
            else:
                print("No value entered — skipping.")
        else:
            print("Skipping — you can set GITHUB_PAT in .env later.")

    print()


def resolve_target(args: argparse.Namespace) -> str:
    """Return '--all' or the project name from the mutually exclusive group."""
    return "--all" if args.all else args.project


def for_containers(action: str, target: str) -> None:
    """Run a docker action on one project or --all."""
    if target == "--all":
        containers = get_agent_containers()
        if not containers:
            print("No sandbox agent containers found.")
            return
        for name in containers:
            print(f"{action} {name}...")
            run(["docker", action, name])
    else:
        container = f"sandbox-agent-{target}"
        if not container_exists(container):
            die(f"Container {container} not found.")
        run(["docker", action, container])


# ─── Commands ─────────────────────────────────────────────────────────────────


def cmd_setup(args: argparse.Namespace) -> None:
    ensure_env_file()
    prompt_env_settings()
    cfg = load_config()

    print("=== Sandbox Setup ===")

    if not cfg.github_pat:
        print("Note: GITHUB_PAT not set — mirroring will only work for public repos.")

    # Create projects directory (if configured)
    if cfg.projects_dir:
        Path(cfg.projects_dir).mkdir(parents=True, exist_ok=True)
        print(f"Projects directory: {cfg.projects_dir}")
    else:
        print("Projects directory: (standard Docker volumes)")

    # Generate Gitea SECRET_KEY if not already set
    if not os.environ.get("GITEA_SECRET_KEY"):
        secret_key = secrets.token_hex(32)
        update_env_key("GITEA_SECRET_KEY", secret_key)
        os.environ["GITEA_SECRET_KEY"] = secret_key
        print("Generated Gitea SECRET_KEY.")

    # Start infrastructure (reviewer managed separately via 'fetch-sandbox.py setup')
    print("Starting infrastructure (Gitea, router)...")
    docker_compose("up", "-d", "--build", "gitea", "router")

    wait_for_gitea(cfg)

    # Create Gitea admin user and token if needed
    if not cfg.gitea_admin_token:
        print("Creating Gitea admin user...")
        admin_pass = gen_password()

        result = run(["docker", "exec", "-u", "git", "sandbox-gitea", "gitea", "admin", "user", "create",
             "--admin", "--username", "sandbox-admin", "--password", admin_pass,
             "--email", "admin@sandbox.local", "--must-change-password=false"],
            capture_output=True, text=True)
        if result.returncode != 0:
            # Code 1 with "already exists" is fine (idempotent re-run)
            if "already exists" not in (result.stderr + result.stdout):
                die(f"Failed to create Gitea admin user: {result.stderr.strip()}")

        url = f"http://localhost:{cfg.gitea_port}/api/v1/users/sandbox-admin/tokens"
        try:
            resp = http_basic_auth_post(url, "sandbox-admin", admin_pass, {
                "name": "sandbox-cli",
                "scopes": ["all"],
            })
        except (HTTPError, URLError) as e:
            die(f"Failed to generate Gitea admin token: {e}")

        token = resp.get("sha1") or resp.get("token") or ""
        if not token:
            die(f"Failed to generate Gitea admin token: {resp}")

        cfg.gitea_admin_token = token

        cfg.gitea_admin_password = admin_pass

        # Append to .env
        env_file = SCRIPT_DIR / ".env"
        with env_file.open("a") as f:
            f.write(f"\nGITEA_ADMIN_PASSWORD={admin_pass}")
            f.write(f"\nGITEA_ADMIN_TOKEN={token}\n")
        print("Gitea admin credentials saved to .env")
    else:
        print("Gitea admin token already configured.")

    print(f"""
=== Setup Complete ===
Gitea UI:      http://localhost:{cfg.gitea_port}/explore/repos?sort=newest&type=fork
Gitea login:   sandbox-admin / {cfg.gitea_admin_password}
Projects dir:  {cfg.projects_dir}
""")


def cmd_create(args: argparse.Namespace) -> None:
    cfg = load_config()
    if args.docker and not sysbox_available():
        die("--docker requires Sysbox runtime. See: https://github.com/nestybox/sysbox#installation")
    project = parse_project_name(args.github_url)
    container_name = f"sandbox-agent-{project}"
    gitea_user = f"agent-{project}"
    volume_name = f"sandbox-{project}"

    print(f"=== Creating sandbox: {project} ===")

    # 1. Create Gitea mirror
    print(f"Mirroring {args.github_url} to Gitea...")
    if gitea_api_ok(cfg, "GET", f"/repos/sandbox-admin/{project}"):
        print("Mirror already exists, triggering sync...")
        gitea_api_ok(cfg, "POST", f"/repos/sandbox-admin/{project}/mirror-sync")
    else:
        migrate_payload = {
            "clone_addr": args.github_url,
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
                 {"description": f"Read-only mirror of {args.github_url}"})

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
    gitea_api_ok(cfg, "PUT", f"/repos/{gitea_user}/{project}/subscription")

    # Determine base branch: --branch flag → Gitea mirror's default_branch
    base_branch = args.branch
    if not base_branch:
        repo_info = gitea_api_or(cfg, "GET", f"/repos/sandbox-admin/{project}", {})
        if isinstance(repo_info, dict):
            base_branch = repo_info.get("default_branch", "")
        if base_branch:
            print(f"Base branch: {base_branch} (from repo default)")

    # 3. Generate fresh Gitea token
    print(f"Generating Gitea token for {gitea_user}...")
    agent_token = generate_gitea_token(cfg, gitea_user, user_pass)

    # 4. Build agent image if needed
    profile = args.profile or cfg.default_profile
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

    # 6. Create Docker volume
    if not run_quiet(["docker", "volume", "inspect", volume_name]):
        if cfg.projects_dir:
            workspace_dir = Path(cfg.projects_dir) / project
            workspace_dir.mkdir(parents=True, exist_ok=True)
            run_check(["docker", "volume", "create", "--driver", "local",
                        "--opt", "type=none", "--opt", f"device={workspace_dir}",
                        "--opt", "o=bind", volume_name])
        else:
            run_check(["docker", "volume", "create", volume_name])

    # Copy container/ files to agent home and fix ownership for bind mounts.
    # Use host GID so the invoking user gets group rw access to container_volumes/.
    host_gid = os.getgid()
    container_src = SCRIPT_DIR / "container"
    if container_src.is_dir():
        run_check(["docker", "run", "--rm", "-v", f"{volume_name}:/home/agent",
                    "-v", f"{container_src}:/src:ro", "alpine",
                    "sh", "-c", f"cp /src/* /home/agent/ && chmod +x /home/agent/*.sh 2>/dev/null; chown -R 1000:{host_gid} /home/agent && chmod 2770 /home/agent"])
    else:
        run_check(["docker", "run", "--rm", "-v", f"{volume_name}:/home/agent", "alpine",
                    "sh", "-c", f"chown -R 1000:{host_gid} /home/agent && chmod 2770 /home/agent"])

    # 7. Create per-project network and connect infrastructure
    print("Setting up agent network...")
    open_egress = args.open_egress or cfg.default_open_egress
    agent_network, router_ip = ensure_agent_network(project, cfg, open_egress)

    # 8. Remove existing container
    if container_exists(container_name):
        print("Removing existing container...")
        run(["docker", "rm", "-f", container_name], capture_output=True)

    # 9. Start agent container
    print("Starting agent container...")

    ssh_port = args.ssh_port or find_free_port(2222)
    ssh_pass = gen_password()
    memory = args.memory or cfg.default_memory

    docker_args = build_agent_docker_args(
        container_name=container_name, project=project, network=agent_network,
        volume_name=volume_name, ssh_port=ssh_port, agent_token=agent_token,
        gitea_user=gitea_user, ssh_pass=ssh_pass,
        dns_servers=cfg.dns_servers, memory=memory, open_egress=open_egress, image=image,
        branch=base_branch or "", cpus=args.cpus or "",
        gpus=args.gpus or profile_default_gpus(profile), claude_yolo=args.claude_yolo,
        docker=args.docker,
    )
    run_check(["docker", *docker_args])

    # 10. Inject default route through the router
    print("Injecting network route...")
    inject_route(container_name, router_ip)

    # 11. Install Claude Code if --claude-yolo (needs network, so after route injection)
    if args.claude_yolo:
        install_claude_code(container_name)

    # 12. Install Docker-in-Docker if --docker (needs network, so after route injection)
    if args.docker:
        install_docker_dind(container_name)

    # 13. Start byobu session (after all installs so shell inherits final environment)
    start_byobu_session(container_name)

    egress_label = "open (all ports)" if open_egress else "locked (80/443/DNS only)"
    docker_label = " (Docker-in-Docker)" if args.docker else ""
    branch_label = f"\nBase branch: {base_branch}" if base_branch else ""
    print(f"""
=== Sandbox ready: {project} ==={docker_label}
Attach:    sandbox attach {project}
SSH:       ssh agent@localhost -p {ssh_port}  (password: {ssh_pass})
Gitea:     http://localhost:{cfg.gitea_port}/{gitea_user}/{project}
Gitea login: sandbox-admin / {cfg.gitea_admin_password}
Egress:    {egress_label}{branch_label}

To review agent work from your real repo:
  git remote add staging http://localhost:{cfg.gitea_port}/{gitea_user}/{project}.git
  python fetch-sandbox.py <repo-path> <branch-name>""")


def cmd_attach(args: argparse.Namespace) -> None:
    container = f"sandbox-agent-{args.project}"
    if not container_running(container):
        die(f"Container {container} is not running. Run: sandbox start {args.project}")
    # Recreate byobu session if it was destroyed (e.g. user typed exit instead of F6)
    r = subprocess.run(
        ["docker", "exec", container, "byobu", "has-session", "-t", "main"],
        capture_output=True,
    )
    if r.returncode != 0:
        subprocess.run(
            ["docker", "exec", "-d", container,
             "byobu", "new-session", "-d", "-s", "main", "-c", "/home/agent", "exec bash"],
            capture_output=True,
        )
    print(f"Attaching to {args.project} byobu session (F6 to detach)...")
    os.execvp("docker", ["docker", "exec", "-it", container, "byobu", "attach", "-t", "main"])


def cmd_ssh(args: argparse.Namespace) -> None:
    containers = get_agent_containers()
    if not containers:
        die("No sandbox agent containers found.")
    for name in containers:
        project = name.removeprefix("sandbox-agent-")
        r = subprocess.run(
            ["docker", "inspect", "-f",
             "{{.State.Status}}\t"
             "{{range .Config.Env}}{{println .}}{{end}}", name],
            capture_output=True, text=True,
        )
        lines = r.stdout.strip().split("\t", 1)
        state = lines[0] if lines else "?"
        ssh_pass = ""
        if len(lines) > 1:
            for line in lines[1].splitlines():
                if line.startswith("SSH_PASSWORD="):
                    ssh_pass = line.split("=", 1)[1]
                    break
        r = subprocess.run(
            ["docker", "inspect", "-f",
             "{{range $p, $binds := .HostConfig.PortBindings}}"
             "{{range $binds}}{{.HostPort}}{{end}}{{end}}", name],
            capture_output=True, text=True,
        )
        ssh_port = r.stdout.strip() or "-"
        print(f"{project}  ({state})")
        print(f"  ssh agent@localhost -p {ssh_port}")
        print(f"  password: {ssh_pass}")
        print()


def cmd_stop(args: argparse.Namespace) -> None:
    for_containers("stop", resolve_target(args))


def cmd_start(args: argparse.Namespace) -> None:
    cfg = load_config()
    target = resolve_target(args)
    if target == "--all":
        containers = get_agent_containers()
        if not containers:
            print("No sandbox agent containers found.")
            return
        for name in containers:
            print(f"start {name}...")
            run(["docker", "start", name])
            _reinject_route(name, cfg)
            start_byobu_session(name)
    else:
        container = f"sandbox-agent-{target}"
        if not container_exists(container):
            die(f"Container {container} not found.")
        run(["docker", "start", container])
        _reinject_route(container, cfg)
        start_byobu_session(container)


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


def cmd_pause(args: argparse.Namespace) -> None:
    for_containers("pause", resolve_target(args))


def cmd_unpause(args: argparse.Namespace) -> None:
    for_containers("unpause", resolve_target(args))


def cmd_sync(args: argparse.Namespace) -> None:
    cfg = load_config()
    project = args.project
    print(f"Triggering mirror sync for {project}...")
    gitea_api_ok(cfg, "POST", f"/repos/sandbox-admin/{project}/mirror-sync")

    container = f"sandbox-agent-{project}"
    if container_running(container):
        print("Pulling latest in container...")
        run(["docker", "exec", container, "bash", "-c",
             f"cd /home/agent/{project} && git pull --ff-only"], capture_output=True)

    print("Sync complete.")


def cmd_set_branch(args: argparse.Namespace) -> None:
    """Switch the base branch for an agent sandbox without recreating it."""
    project = args.project
    branch = args.branch
    container_name = f"sandbox-agent-{project}"

    if not container_running(container_name):
        die(f"Container {container_name} is not running. Run: sandbox start {project}")

    home = f"/home/agent"
    repo_dir = f"{home}/{project}"

    # 1. Verify branch exists in upstream
    print(f"Verifying branch '{branch}' exists in upstream...")
    r = run(["docker", "exec", container_name, "bash", "-c",
             f"cd {repo_dir} && git fetch upstream && git rev-parse upstream/{branch}"],
            capture_output=True, text=True)
    if r.returncode != 0:
        die(f"Branch '{branch}' not found in upstream mirror. "
            f"Push it to GitHub first, then run: sandbox sync {project}")

    # 2. Re-render templates from saved .template files
    print(f"Updating agent instructions to use '{branch}' as base branch...")
    for filename in ["CLAUDE.md", "repo-watch-prompt.md"]:
        template = f"{home}/.{filename}.template"
        target = f"{home}/{filename}"
        render_cmd = (
            f"if [ -f '{template}' ]; then "
            f"cp '{template}' '{target}' && "
            f"sed -i 's/{{{{BASE_BRANCH}}}}/{branch}/g' '{target}'; "
            f"fi"
        )
        run(["docker", "exec", container_name, "bash", "-c", render_cmd],
            capture_output=True)

    # 3. Update env var in /etc/profile.d/sandbox-env.sh
    env_cmd = (
        f"sudo sed -i 's/^export BASE_BRANCH=.*/export BASE_BRANCH=\"{branch}\"/' "
        f"/etc/profile.d/sandbox-env.sh"
    )
    run(["docker", "exec", container_name, "bash", "-c", env_cmd], capture_output=True)

    # 4. Checkout the branch if agent is currently on the old base (not a feature branch)
    r = run(["docker", "exec", container_name, "bash", "-c",
             f"cd {repo_dir} && git symbolic-ref --short HEAD"],
            capture_output=True, text=True)
    current_branch = r.stdout.strip() if r.returncode == 0 else ""
    if current_branch and not current_branch.startswith("agent/"):
        print(f"Checking out '{branch}'...")
        run(["docker", "exec", container_name, "bash", "-c",
             f"cd {repo_dir} && git checkout {branch}"],
            capture_output=True)

    print(f"Base branch updated to '{branch}'. Next agent invocation will use it.")



def cmd_recreate(args: argparse.Namespace) -> None:
    cfg = load_config()
    if args.docker and not sysbox_available():
        die("--docker requires Sysbox runtime. See: https://github.com/nestybox/sysbox#installation")
    project = args.project
    container_name = f"sandbox-agent-{project}"
    gitea_user = f"agent-{project}"
    volume_name = f"sandbox-{project}"

    if not run_quiet(["docker", "volume", "inspect", volume_name]):
        die(f"Project {project} does not exist.")

    # Confirmation
    print(f"=== Recreate: {project} ===\n")
    print("This will:")
    print(f"  - Remove the agent container (sandbox-agent-{project})")
    print(f"  - Delete the workspace volume and all agent work")
    print(f"  - Generate fresh Gitea credentials")
    print(f"  - Start a new container with a fresh clone\n")
    print("Preserved:")
    print(f"  - Gitea agent user and repo (agent-{project}/{project})")
    print(f"  - Gitea mirror (sandbox-admin/{project})\n")
    confirm = input("Type 'yes' to confirm: ").strip()
    if confirm != "yes":
        print("Aborted.")
        return

    # Stop existing container
    print("Stopping existing container...")
    run(["docker", "rm", "-f", container_name], capture_output=True)

    # Remove existing volume and workspace directory
    print("Removing workspace volume...")
    run(["docker", "volume", "rm", volume_name], capture_output=True)
    if cfg.projects_dir:
        workspace_dir = Path(cfg.projects_dir) / project
        if workspace_dir.is_dir():
            print("Removing workspace directory...")
            try:
                shutil.rmtree(workspace_dir)
            except PermissionError:
                run(["docker", "run", "--rm", "-v", f"{workspace_dir.resolve()}:/mnt/ws",
                     "alpine", "rm", "-rf", "/mnt/ws"], capture_output=True)
                if workspace_dir.is_dir():
                    workspace_dir.rmdir()

    # Recreate volume
    print("Creating fresh volume...")
    if cfg.projects_dir:
        workspace_dir = Path(cfg.projects_dir) / project
        workspace_dir.mkdir(parents=True, exist_ok=True)
        run_check(["docker", "volume", "create", "--driver", "local",
                    "--opt", "type=none", "--opt", f"device={workspace_dir}",
                    "--opt", "o=bind", volume_name])
    else:
        run_check(["docker", "volume", "create", volume_name])

    # Copy container/ files to agent home and fix ownership
    host_gid = os.getgid()
    container_src = SCRIPT_DIR / "container"
    if container_src.is_dir():
        run_check(["docker", "run", "--rm", "-v", f"{volume_name}:/home/agent",
                    "-v", f"{container_src}:/src:ro", "alpine",
                    "sh", "-c", f"cp /src/* /home/agent/ && chmod +x /home/agent/*.sh 2>/dev/null; chown -R 1000:{host_gid} /home/agent && chmod 2770 /home/agent"])
    else:
        run_check(["docker", "run", "--rm", "-v", f"{volume_name}:/home/agent", "alpine",
                    "sh", "-c", f"chown -R 1000:{host_gid} /home/agent && chmod 2770 /home/agent"])

    # Generate fresh Gitea token
    print("Generating fresh Gitea token...")
    user_pass = gen_password()
    gitea_api(cfg, "PATCH", f"/admin/users/{gitea_user}", {
        "login_name": gitea_user,
        "source_id": 0,
        "password": user_pass,
        "must_change_password": False,
    })
    agent_token = generate_gitea_token(cfg, gitea_user, user_pass)

    # Start new container
    profile = args.profile or cfg.default_profile
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

    # Ensure per-project network exists
    open_egress = args.open_egress or cfg.default_open_egress
    agent_network, router_ip = ensure_agent_network(project, cfg, open_egress)

    ssh_port = args.ssh_port or find_free_port(2222)
    ssh_pass = gen_password()
    memory = args.memory or cfg.default_memory

    docker_args = build_agent_docker_args(
        container_name=container_name, project=project, network=agent_network,
        volume_name=volume_name, ssh_port=ssh_port, agent_token=agent_token,
        gitea_user=gitea_user, ssh_pass=ssh_pass,
        dns_servers=cfg.dns_servers, memory=memory, open_egress=open_egress, image=image,
        branch=args.branch or "", cpus=args.cpus or "",
        gpus=args.gpus or profile_default_gpus(profile), claude_yolo=args.claude_yolo,
        docker=args.docker,
    )

    print("Starting new container...")
    run_check(["docker", *docker_args])

    # Inject default route through the router
    print("Injecting network route...")
    inject_route(container_name, router_ip)

    # Install Claude Code if --claude-yolo (needs network, so after route injection)
    if args.claude_yolo:
        install_claude_code(container_name)

    # Install Docker-in-Docker if --docker (needs network, so after route injection)
    if args.docker:
        install_docker_dind(container_name)

    # Start byobu session (after all installs so shell inherits final environment)
    start_byobu_session(container_name)

    print(f"""
=== Recreated: {project} ===
Attach:  sandbox attach {project}
SSH:     ssh agent@localhost -p {ssh_port}  (password: {ssh_pass})""")


def cmd_status(args: argparse.Namespace) -> None:
    cfg = load_config()
    print("=== Sandbox Status ===\n")

    print("── Infrastructure ──")
    for svc in ["sandbox-gitea", "sandbox-router"]:
        r = subprocess.run(["docker", "inspect", "-f", "{{.State.Status}}", svc],
                           capture_output=True, text=True)
        state = r.stdout.strip() if r.returncode == 0 else "not found"
        print(f"  {svc:<20s} {state}")
    if cfg.gitea_admin_password:
        print(f"\n  Gitea UI:    http://localhost:{cfg.gitea_port}")
        print(f"  Gitea login: sandbox-admin / {cfg.gitea_admin_password}")

    print("\n── Agent Containers ──")
    containers = get_agent_containers()
    if not containers:
        print("  (no agent containers)")
    else:
        # Collect data first to compute column widths
        rows = []
        for name in containers:
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
            rows.append((project_name, state, ssh_port))
        name_width = max(len(r[0]) for r in rows) + 4
        name_width = max(name_width, len("PROJECT"))
        print(f"  {'PROJECT':<{name_width}s}  {'STATE':<10s}  SSH PORT")
        for project_name, state, ssh_port in rows:
            print(f"  {project_name:<{name_width}s}  {state:<10s}  {ssh_port}")

    if cfg.projects_dir:
        print(f"\n── Projects Directory ──\n  {cfg.projects_dir}")
        projects_path = Path(cfg.projects_dir)
        if projects_path.is_dir():
            for d in sorted(projects_path.iterdir()):
                if d.is_dir():
                    print(f"    {d.name}/")
    else:
        print("\n── Projects Directory ──\n  (standard Docker volumes)")


def cmd_destroy(args: argparse.Namespace) -> None:
    cfg = load_config()
    project = args.project
    container_name = f"sandbox-agent-{project}"
    gitea_user = f"agent-{project}"
    volume_name = f"sandbox-{project}"

    print(f"=== Destroy: {project} ===\n")
    print("This will permanently delete:")
    print(f"  - Agent container (sandbox-agent-{project})")
    print(f"  - Workspace volume and all agent work")
    print(f"  - Agent network")
    print(f"  - Gitea agent user and repo (agent-{project}/{project})")
    print(f"  - Gitea mirror (sandbox-admin/{project})\n")
    confirm = input("Type 'yes' to confirm: ").strip()
    if confirm != "yes":
        print("Aborted.")
        return

    print(f"\nDestroying sandbox: {project}...")

    if container_exists(container_name):
        print("Removing container...")
        run(["docker", "rm", "-f", container_name], capture_output=True)

    if run_quiet(["docker", "volume", "inspect", volume_name]):
        print("Removing Docker volume...")
        run(["docker", "volume", "rm", volume_name], capture_output=True)

    if cfg.projects_dir:
        workspace_dir = Path(cfg.projects_dir) / project
        if workspace_dir.is_dir():
            print("Removing workspace directory...")
            try:
                shutil.rmtree(workspace_dir)
            except PermissionError:
                # Files created inside containers are owned by root
                run(["docker", "run", "--rm", "-v", f"{workspace_dir.resolve()}:/mnt/ws",
                     "alpine", "rm", "-rf", "/mnt/ws"], capture_output=True)
                if workspace_dir.is_dir():
                    workspace_dir.rmdir()

    # Remove per-project network
    print("Removing agent network...")
    remove_agent_network(project)

    if gitea_api_ok(cfg, "GET", f"/users/{gitea_user}"):
        print(f"Removing Gitea user {gitea_user}...")
        gitea_api_ok(cfg, "DELETE", f"/admin/users/{gitea_user}?purge=true")

    if gitea_api_ok(cfg, "GET", f"/repos/sandbox-admin/{project}"):
        print(f"Removing Gitea mirror (sandbox-admin/{project})...")
        gitea_api_ok(cfg, "DELETE", f"/repos/sandbox-admin/{project}")

    print(f"Destroyed.")


def cmd_unsetup(args: argparse.Namespace) -> None:
    cfg = load_config()

    containers = get_agent_containers()
    projects = [n.removeprefix("sandbox-agent-") for n in containers]

    print("=== Sandbox Teardown ===\n")
    print("This will permanently destroy:")
    if projects:
        for p in projects:
            print(f"  - Agent container, volume, and network for: {p}")
    else:
        print("  - (no agent containers found)")
    print("  - Gitea server and all mirrored/forked repos")
    print("  - Router")
    print("  - All associated Docker volumes\n")

    confirm = input("Type 'yes' to confirm: ").strip()
    if confirm != "yes":
        print("Aborted.")
        return

    print("\n=== Tearing down sandbox infrastructure ===\n")

    # 1. Destroy all agent containers, volumes, networks, and Gitea users
    if containers:
        print("── Destroying all agent projects ──")
        for name in containers:
            project = name.removeprefix("sandbox-agent-")
            gitea_user = f"agent-{project}"
            volume_name = f"sandbox-{project}"

            print(f"  Removing {name}...")
            run(["docker", "rm", "-f", name], capture_output=True)

            if run_quiet(["docker", "volume", "inspect", volume_name]):
                run(["docker", "volume", "rm", volume_name], capture_output=True)

            if cfg.projects_dir:
                workspace_dir = Path(cfg.projects_dir) / project
                if workspace_dir.is_dir():
                    try:
                        shutil.rmtree(workspace_dir)
                    except PermissionError:
                        run(["docker", "run", "--rm", "-v", f"{workspace_dir.resolve()}:/mnt/ws",
                             "alpine", "rm", "-rf", "/mnt/ws"], capture_output=True)
                        if workspace_dir.is_dir():
                            workspace_dir.rmdir()

            remove_agent_network(project)
    else:
        print("No agent containers found.")

    # 2. Stop and remove infrastructure containers + volumes
    print("\n── Removing infrastructure ──")
    docker_compose("down", "-v")

    # 4. Remove generated tokens from .env
    env_file = SCRIPT_DIR / ".env"
    cleanup_prefixes = ("GITEA_ADMIN_TOKEN=", "GITEA_ADMIN_PASSWORD=", "GITEA_SECRET_KEY=")
    if env_file.exists():
        lines = env_file.read_text().splitlines()
        new_lines = [l for l in lines
                     if not any(l.strip().startswith(p) for p in cleanup_prefixes)]
        if len(new_lines) != len(lines):
            env_file.write_text("\n".join(new_lines) + "\n")
            print("Removed generated tokens from .env")

    print("""
=== Teardown complete ===
All containers, volumes, networks, and Gitea data have been removed.
Your .env configuration (except GITEA_ADMIN_TOKEN) is preserved.
Run 'sandbox setup' to start fresh.""")


def cmd_logs(args: argparse.Namespace) -> None:
    container = f"sandbox-agent-{args.project}"
    os.execvp("docker", ["docker", "logs", "-f", container])


# ─── CLI Parser ───────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sandbox", description="LLM Agent Sandbox CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    # Shared flags for create/recreate
    container_flags = argparse.ArgumentParser(add_help=False)
    container_flags.add_argument("--branch", default="")
    container_flags.add_argument("--open-egress", action="store_true")
    container_flags.add_argument("--memory", default="")
    container_flags.add_argument("--cpus", default="")
    container_flags.add_argument("--gpus", default="")
    container_flags.add_argument("--profile", default="")
    container_flags.add_argument("--ssh-port", type=int, default=0)
    container_flags.add_argument("--claude-yolo", action="store_true")
    container_flags.add_argument("--docker", action="store_true",
                                 help="Enable Docker-in-Docker via Sysbox runtime")

    sub.add_parser("setup", help="One-time infrastructure setup").set_defaults(func=cmd_setup)
    sub.add_parser("unsetup", help="Tear down all infrastructure, containers, and volumes").set_defaults(func=cmd_unsetup)

    p = sub.add_parser("create", help="Mirror repo and spin up agent container",
                       parents=[container_flags])
    p.add_argument("github_url", metavar="github-url")
    p.set_defaults(func=cmd_create)

    p = sub.add_parser("attach", help="Attach to agent's byobu session")
    p.add_argument("project")
    p.set_defaults(func=cmd_attach)

    sub.add_parser("ssh", help="Show SSH connection info for all containers").set_defaults(func=cmd_ssh)

    for name, help_text in [("stop", "Stop"), ("start", "Start"),
                            ("pause", "Freeze"), ("unpause", "Resume")]:
        p = sub.add_parser(name, help=f"{help_text} agent container(s)")
        g = p.add_mutually_exclusive_group(required=True)
        g.add_argument("project", nargs="?", help="project name")
        g.add_argument("--all", action="store_true", help="all containers")
        p.set_defaults(func={"stop": cmd_stop, "start": cmd_start,
                              "pause": cmd_pause, "unpause": cmd_unpause}[name])

    p = sub.add_parser("sync", help="Trigger Gitea mirror sync")
    p.add_argument("project")
    p.set_defaults(func=cmd_sync)

    p = sub.add_parser("set-branch", help="Switch agent's base branch without recreating")
    p.add_argument("project")
    p.add_argument("branch", help="Branch name to use as the new base")
    p.set_defaults(func=cmd_set_branch)

    p = sub.add_parser("recreate", help="New container + fresh token, keeps volume",
                       parents=[container_flags])
    p.add_argument("project")
    p.set_defaults(func=cmd_recreate)

    sub.add_parser("status", help="List all projects and containers").set_defaults(func=cmd_status)

    p = sub.add_parser("destroy", help="Remove container, volume, Gitea user + fork")
    p.add_argument("project")
    p.set_defaults(func=cmd_destroy)

    p = sub.add_parser("logs", help="Tail container logs")
    p.add_argument("project")
    p.set_defaults(func=cmd_logs)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        args.func(args)
    except KeyboardInterrupt:
        sys.exit(130)
    except RuntimeError as e:
        die(str(e))


if __name__ == "__main__":
    main()
