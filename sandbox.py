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
        self.agent_api_key = ""
        self.reviewer_enabled = True
        self.reviewer_api_key = ""
        self.gitea_admin_token = ""
        self.projects_dir = ""
        self.gitea_port = "3000"
        self.default_memory = ""
        self.default_open_egress = False
        self.default_image = "sandbox-agent:latest"


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
    cfg.agent_api_key = os.environ.get("SANDBOX_CLAUDE_KEY", "")
    cfg.reviewer_enabled = os.environ.get("REVIEWER_ENABLED", "true").lower() != "false"
    cfg.reviewer_api_key = os.environ.get("REVIEWER_API_KEY", "")
    cfg.gitea_admin_token = os.environ.get("GITEA_ADMIN_TOKEN", "")
    cfg.gitea_port = os.environ.get("GITEA_PORT", "3000")
    cfg.projects_dir = os.environ.get("PROJECTS_DIR", "./container_volumes/")

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


def docker_compose(*args: str) -> None:
    run_check([
        "docker", "compose",
        "-f", str(SCRIPT_DIR / "docker-compose.yml"),
        "--env-file", str(SCRIPT_DIR / ".env"),
        *args,
    ])


def build_agent_docker_args(
    *,
    container_name: str,
    project: str,
    network: str,
    volume_name: str,
    ssh_port: int,
    agent_token: str,
    gitea_user: str,
    install_claude: bool,
    ssh_pass: str,
    proxy_port: int,
    memory: str,
    open_egress: bool,
    image: str,
    branch: str = "",
    cpus: str = "",
    gpus: str = "",
    agent_api_key: str = "",
) -> list[str]:
    """Build the docker run argument list. Shared by create and recreate."""
    args = [
        "run", "-d",
        "--name", container_name,
        "--network", network,
        "--hostname", project,
        "-v", f"{volume_name}:/workspace",
        "-p", f"{ssh_port}:22",
        "-e", f"GITEA_URL={GITEA_INTERNAL_URL}",
        "-e", f"GITEA_TOKEN={agent_token}",
        "-e", f"GITEA_USER={gitea_user}",
        "-e", f"REPO_NAME={project}",
        "-e", f"INSTALL_CLAUDE={'1' if install_claude else '0'}",
        "-e", f"SSH_PASSWORD={ssh_pass}",
        "-e", f"HTTP_PROXY=http://sandbox-proxy:{proxy_port}",
        "-e", f"HTTPS_PROXY=http://sandbox-proxy:{proxy_port}",
        "-e", f"http_proxy=http://sandbox-proxy:{proxy_port}",
        "-e", f"https_proxy=http://sandbox-proxy:{proxy_port}",
        "-e", "NO_PROXY=sandbox-gitea,sandbox-review,sandbox-proxy,localhost,127.0.0.1",
        "-e", "no_proxy=sandbox-gitea,sandbox-review,sandbox-proxy,localhost,127.0.0.1",
        # Runtime hardening
        "--cap-drop=ALL",
        "--cap-add=CHOWN", "--cap-add=FOWNER", "--cap-add=SETGID",
        "--cap-add=SETUID", "--cap-add=KILL", "--cap-add=FSETID",
        "--cap-add=AUDIT_WRITE",
        "--pids-limit=512",
        "--label", f"sandbox.project={project}",
        "--label", f"sandbox.egress={open_egress}",
    ]
    if branch:
        args += ["-e", f"REPO_BRANCH={branch}"]
    if memory:
        args += [f"--memory={memory}"]
    if cpus:
        args += [f"--cpus={cpus}"]
    if gpus:
        args += [f"--gpus={gpus}"]
    if install_claude and agent_api_key:
        args += ["-e", f"ANTHROPIC_API_KEY={agent_api_key}"]
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


def ensure_agent_network(project: str, cfg: Config) -> str:
    """Create a per-project internal network and connect infrastructure services."""
    network = f"sandbox-net-{project}"
    if not run_quiet(["docker", "network", "inspect", network]):
        run_check(["docker", "network", "create", "--internal", network])
    # Connect infrastructure services (ignore errors if already connected)
    for svc in ["sandbox-gitea", "sandbox-proxy"]:
        run(["docker", "network", "connect", network, svc], capture_output=True)
    if cfg.reviewer_enabled:
        run(["docker", "network", "connect", network, "sandbox-review"], capture_output=True)
    return network


def remove_agent_network(project: str) -> None:
    """Disconnect infrastructure and remove per-project network."""
    network = f"sandbox-net-{project}"
    for svc in ["sandbox-gitea", "sandbox-proxy", "sandbox-review"]:
        run(["docker", "network", "disconnect", network, svc], capture_output=True)
    run(["docker", "network", "rm", network], capture_output=True)


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
    cfg = load_config()

    print("=== Sandbox Setup ===")

    required = [("GITHUB_PAT", cfg.github_pat)]
    if cfg.reviewer_enabled:
        required.append(("REVIEWER_API_KEY", cfg.reviewer_api_key))
    for name, value in required:
        if not value:
            die(f"{name} not set in .env")

    # Create projects directory (if configured)
    if cfg.projects_dir:
        Path(cfg.projects_dir).mkdir(parents=True, exist_ok=True)
        print(f"Projects directory: {cfg.projects_dir}")
    else:
        print("Projects directory: (standard Docker volumes)")

    # Start infrastructure
    if cfg.reviewer_enabled:
        print("Starting infrastructure (Gitea, review service, proxy)...")
        docker_compose("up", "-d", "--build")
    else:
        print("Starting infrastructure (Gitea, proxy — reviewer disabled)...")
        docker_compose("up", "-d", "--build", "gitea", "proxy")

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

        # Append to .env
        env_file = SCRIPT_DIR / ".env"
        with env_file.open("a") as f:
            f.write(f"\nGITEA_ADMIN_TOKEN={token}\n")
        print("Gitea admin token saved to .env")

        # Restart review service to pick up the token (if enabled)
        if cfg.reviewer_enabled:
            docker_compose("up", "-d", "review")
    else:
        print("Gitea admin token already configured.")

    print(f"""
=== Setup Complete ===
Gitea UI:      http://localhost:{cfg.gitea_port}
Projects dir:  {cfg.projects_dir}

Tip: Add sandbox to your PATH:
  ln -sf {SCRIPT_DIR / 'sandbox.py'} ~/.local/bin/sandbox""")


def cmd_create(args: argparse.Namespace) -> None:
    cfg = load_config()
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
        gitea_api(cfg, "POST", "/repos/migrate", {
            "clone_addr": args.github_url,
            "repo_name": project,
            "repo_owner": "sandbox-admin",
            "mirror": True,
            "auth_token": cfg.github_pat,
            "service": "github",
        })
        print("Mirror created. Waiting for initial sync...")
        time.sleep(5)

    # 2. Create per-project Gitea user
    print(f"Setting up Gitea user: {gitea_user}...")
    user_pass = gen_password()

    if not gitea_api_ok(cfg, "GET", f"/users/{gitea_user}"):
        gitea_api(cfg, "POST", "/admin/users", {
            "username": gitea_user,
            "password": user_pass,
            "email": f"{gitea_user}@sandbox.local",
            "must_change_password": False,
            "visibility": "private",
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

    # 3. Generate fresh Gitea token
    print(f"Generating Gitea token for {gitea_user}...")
    agent_token = generate_gitea_token(cfg, gitea_user, user_pass)

    # 4. Create webhook (if reviewer enabled)
    if cfg.reviewer_enabled:
        print("Configuring webhook...")
        hooks = gitea_api_or(cfg, "GET", f"/repos/{gitea_user}/{project}/hooks", [])
        has_hook = any(
            "sandbox-review" in (h.get("config", {}).get("url", ""))
            for h in (hooks if isinstance(hooks, list) else [])
        )
        if not has_hook:
            gitea_api_ok(cfg, "POST", f"/repos/{gitea_user}/{project}/hooks", {
                "type": "gitea",
                "active": True,
                "events": ["push"],
                "config": {
                    "url": "http://sandbox-review:8080/webhook",
                    "content_type": "json",
                },
            })
    else:
        print("Reviewer disabled — skipping webhook.")

    # 5. Build agent image if needed
    image = args.image or cfg.default_image
    if not run_quiet(["docker", "image", "inspect", image]):
        print(f"Building agent image: {image}...")
        run_check(["docker", "build", "-t", image, str(SCRIPT_DIR / "agent")])

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

    # Copy container/ files to workspace and fix ownership for bind mounts
    container_src = SCRIPT_DIR / "container"
    if container_src.is_dir():
        run_check(["docker", "run", "--rm", "-v", f"{volume_name}:/workspace",
                    "-v", f"{container_src}:/src:ro", "alpine",
                    "sh", "-c", "mkdir -p /workspace/.sandbox && cp /src/* /workspace/.sandbox/ && chown -R 1000:1000 /workspace"])
    else:
        run_check(["docker", "run", "--rm", "-v", f"{volume_name}:/workspace", "alpine",
                    "sh", "-c", "chown -R 1000:1000 /workspace"])

    # 7. Create per-project network and connect infrastructure
    print("Setting up agent network...")
    agent_network = ensure_agent_network(project, cfg)

    # 8. Remove existing container
    if container_exists(container_name):
        print("Removing existing container...")
        run(["docker", "rm", "-f", container_name], capture_output=True)

    # 9. Start agent container
    print("Starting agent container...")

    open_egress = args.open_egress or cfg.default_open_egress
    proxy_port = 3129 if open_egress else 3128
    ssh_port = args.ssh_port or find_free_port(2222)
    ssh_pass = gen_password()
    memory = args.memory or cfg.default_memory

    # Inject user's SSH public keys into the volume
    ssh_dir = Path.home() / ".ssh"
    if ssh_dir.is_dir():
        keys = []
        for pub in ssh_dir.glob("*.pub"):
            keys.append(pub.read_text())
        if keys:
            key_data = "".join(keys).replace("\n", "\\n")
            run_check(["docker", "run", "--rm", "-v", f"{volume_name}:/workspace", "alpine",
                        "sh", "-c",
                        f"mkdir -p /workspace/.ssh && printf '{key_data}' > /workspace/.ssh/authorized_keys"])

    docker_args = build_agent_docker_args(
        container_name=container_name, project=project, network=agent_network,
        volume_name=volume_name, ssh_port=ssh_port, agent_token=agent_token,
        gitea_user=gitea_user, install_claude=args.claude, ssh_pass=ssh_pass,
        proxy_port=proxy_port, memory=memory, open_egress=open_egress, image=image,
        branch=args.branch or "", cpus=args.cpus or "",
        gpus=args.gpus or "", agent_api_key=cfg.agent_api_key,
    )
    run_check(["docker", *docker_args])

    egress_label = "open (all ports)" if open_egress else "locked (80/443/DNS only)"
    print(f"""
=== Sandbox ready: {project} ===
Attach:    sandbox attach {project}
SSH:       ssh agent@localhost -p {ssh_port}  (password: {ssh_pass})
Gitea:     http://localhost:{cfg.gitea_port}/{gitea_user}/{project}
Egress:    {egress_label}

To review agent work from your real repo:
  git remote add staging http://localhost:{cfg.gitea_port}/{gitea_user}/{project}.git
  sandbox review {project} <branch-name>""")


def cmd_attach(args: argparse.Namespace) -> None:
    container = f"sandbox-agent-{args.project}"
    if not container_running(container):
        die(f"Container {container} is not running. Run: sandbox start {args.project}")
    print(f"Attaching to {args.project} byobu session (F6 to detach)...")
    os.execvp("docker", ["docker", "exec", "-it", container, "byobu", "attach", "-t", "main"])


def cmd_stop(args: argparse.Namespace) -> None:
    for_containers("stop", args.target)


def cmd_start(args: argparse.Namespace) -> None:
    for_containers("start", args.target)


def cmd_pause(args: argparse.Namespace) -> None:
    for_containers("pause", args.target)


def cmd_unpause(args: argparse.Namespace) -> None:
    for_containers("unpause", args.target)


def cmd_sync(args: argparse.Namespace) -> None:
    cfg = load_config()
    project = args.project
    print(f"Triggering mirror sync for {project}...")
    gitea_api_ok(cfg, "POST", f"/repos/sandbox-admin/{project}/mirror-sync")

    container = f"sandbox-agent-{project}"
    if container_running(container):
        print("Pulling latest in container...")
        run(["docker", "exec", container, "bash", "-c",
             f"cd /workspace/{project} && git pull --ff-only"], capture_output=True)

    print("Sync complete.")


def cmd_review(args: argparse.Namespace) -> None:
    cfg = load_config()
    project = args.project
    branch = args.branch
    gitea_user = f"agent-{project}"

    # Ensure we're in a git repo
    if not run_quiet(["git", "rev-parse", "--is-inside-work-tree"]):
        die("Not inside a git repository. Run this command from your real repo directory.")

    # Check staging remote
    r = subprocess.run(["git", "remote"], capture_output=True, text=True)
    if "staging" not in r.stdout.splitlines():
        die(f"'staging' remote not configured.\n"
            f"Run: git remote add staging http://localhost:{cfg.gitea_port}/{gitea_user}/{project}.git")

    full_branch = f"agent/{branch}"
    ref = f"staging/{full_branch}"

    # 1. Fetch
    print("Fetching from staging...")
    run_check(["git", "fetch", "staging"])

    if not run_quiet(["git", "rev-parse", ref]):
        print(f"Error: Branch {full_branch} not found on staging remote.", file=sys.stderr)
        print("Available agent branches:")
        r = subprocess.run(["git", "branch", "-r"], capture_output=True, text=True)
        for line in r.stdout.splitlines():
            if "staging/agent/" in line:
                print(f"  {line.strip().replace('staging/agent/', '')}")
        sys.exit(1)

    print(f"""
{'=' * 64}
  Review: {project} / {full_branch}
{'=' * 64}""")

    # 2. Security review from Gitea
    print("\n── Security Review (automated) ──")
    r = subprocess.run(["git", "rev-parse", ref], capture_output=True, text=True)
    latest_sha = r.stdout.strip()

    comments = gitea_api_or(cfg, "GET",
                            f"/repos/{gitea_user}/{project}/git/commits/{latest_sha}/comments", [])
    found_review = False
    if isinstance(comments, list):
        for c in comments:
            body = c.get("body", "")
            if body.startswith("## Security Review"):
                print(body)
                found_review = True
    if not found_review:
        print("(No automated security review found for this commit)")

    # 3. Pre-merge safety checks
    print("\n── Pre-Merge Safety Checks ──")

    # 3a. Symlinks
    r = subprocess.run(["git", "ls-tree", "-r", "--full-tree", ref],
                       capture_output=True, text=True)
    symlinks = [line for line in r.stdout.splitlines() if line.startswith("120000")]
    if symlinks:
        print("\n\u26a0  SYMLINKS DETECTED:")
        for line in symlinks:
            parts = line.split(None, 3)
            if len(parts) == 4:
                _, _, blob_hash, path = parts
                target_r = subprocess.run(["git", "cat-file", "-p", blob_hash],
                                          capture_output=True, text=True)
                print(f"  {path} -> {target_r.stdout.strip()}")
        print()
    else:
        print("  Symlinks: none")

    # 3b. Auto-execute files
    auto_exec_paths = [
        ".envrc", ".vscode/", ".husky/", ".pre-commit-config.yaml", ".gitmodules",
        "package.json", "setup.py", "setup.cfg", "Makefile", "CMakeLists.txt",
        ".cargo/config.toml", ".eslintrc.js", ".prettierrc.js",
    ]

    r = subprocess.run(["git", "symbolic-ref", "refs/remotes/origin/HEAD"],
                       capture_output=True, text=True)
    base_branch = r.stdout.strip().replace("refs/remotes/origin/", "") if r.returncode == 0 else "main"

    r = subprocess.run(
        ["git", "diff", f"{base_branch}...{ref}", "--", *auto_exec_paths],
        capture_output=True, text=True,
    )
    if r.stdout.strip():
        print(f"\n\u26a0  AUTO-EXECUTE FILES MODIFIED (review these first!):")
        print("\u2500" * 40)
        print(r.stdout)
        print("\u2500" * 40)
    else:
        print("  Auto-execute files: unchanged")

    # 4. Diffstat
    print("\n── Diffstat ──")
    run(["git", "diff", "--stat", f"{base_branch}...{ref}"])

    print(f"""
{'=' * 64}

Review the full diff with:
  git diff {base_branch}...{ref}
  git diff {base_branch}...{ref} -- src/  # specific path
  git log {base_branch}..{ref}            # commit history

Merge when ready:
  git merge --squash {ref}
  git commit""")


def cmd_recreate(args: argparse.Namespace) -> None:
    cfg = load_config()
    project = args.project
    container_name = f"sandbox-agent-{project}"
    gitea_user = f"agent-{project}"
    volume_name = f"sandbox-{project}"

    if not run_quiet(["docker", "volume", "inspect", volume_name]):
        die(f"Project {project} does not exist.")

    # Stop existing container
    print("Stopping existing container...")
    run(["docker", "rm", "-f", container_name], capture_output=True)

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
    image = args.image or cfg.default_image
    if not run_quiet(["docker", "image", "inspect", image]):
        print(f"Building agent image: {image}...")
        run_check(["docker", "build", "-t", image, str(SCRIPT_DIR / "agent")])

    # Ensure per-project network exists
    agent_network = ensure_agent_network(project, cfg)

    open_egress = args.open_egress or cfg.default_open_egress
    proxy_port = 3129 if open_egress else 3128
    ssh_port = args.ssh_port or find_free_port(2222)
    ssh_pass = gen_password()
    memory = args.memory or cfg.default_memory

    docker_args = build_agent_docker_args(
        container_name=container_name, project=project, network=agent_network,
        volume_name=volume_name, ssh_port=ssh_port, agent_token=agent_token,
        gitea_user=gitea_user, install_claude=args.claude, ssh_pass=ssh_pass,
        proxy_port=proxy_port, memory=memory, open_egress=open_egress, image=image,
        branch=args.branch or "", cpus=args.cpus or "",
        gpus=args.gpus or "", agent_api_key=cfg.agent_api_key,
    )

    print("Starting new container...")
    run_check(["docker", *docker_args])

    print(f"""
=== Recreated: {project} ===
Attach:  sandbox attach {project}
SSH:     ssh agent@localhost -p {ssh_port}  (password: {ssh_pass})
Workspace volume preserved.""")


def cmd_status(args: argparse.Namespace) -> None:
    cfg = load_config()
    print("=== Sandbox Status ===\n")

    print("── Infrastructure ──")
    for svc in ["sandbox-gitea", "sandbox-review", "sandbox-proxy"]:
        r = subprocess.run(["docker", "inspect", "-f", "{{.State.Status}}", svc],
                           capture_output=True, text=True)
        state = r.stdout.strip() if r.returncode == 0 else "not found"
        print(f"  {svc:<20s} {state}")

    print("\n── Agent Containers ──")
    containers = get_agent_containers()
    if not containers:
        print("  (no agent containers)")
    else:
        print(f"  {'NAME':<30s} {'STATE':<12s} {'STATUS':<25s} PORTS")
        for name in containers:
            r = subprocess.run(
                ["docker", "inspect", "-f",
                 "{{.State.Status}}\t{{.State.Status}}\t{{range .NetworkSettings.Ports}}"
                 "{{range .}}{{.HostPort}}{{end}}{{end}}", name],
                capture_output=True, text=True,
            )
            parts = r.stdout.strip().split("\t") if r.returncode == 0 else ["?", "?", "?"]
            while len(parts) < 3:
                parts.append("")
            print(f"  {name:<30s} {parts[0]:<12s} {parts[1]:<25s} {parts[2]}")

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

    print(f"=== Destroying sandbox: {project} ===")

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
            shutil.rmtree(workspace_dir)

    # Remove per-project network
    print("Removing agent network...")
    remove_agent_network(project)

    if gitea_api_ok(cfg, "GET", f"/users/{gitea_user}"):
        print(f"Removing Gitea user {gitea_user}...")
        gitea_api_ok(cfg, "DELETE", f"/admin/users/{gitea_user}?purge=true")

    print(f"Destroyed. Gitea mirror (sandbox-admin/{project}) and review comments preserved.")


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
    container_flags.add_argument("--claude", action="store_true")
    container_flags.add_argument("--open-egress", action="store_true")
    container_flags.add_argument("--memory", default="")
    container_flags.add_argument("--cpus", default="")
    container_flags.add_argument("--gpus", default="")
    container_flags.add_argument("--image", default="")
    container_flags.add_argument("--ssh-port", type=int, default=0)

    sub.add_parser("setup", help="One-time infrastructure setup").set_defaults(func=cmd_setup)

    p = sub.add_parser("create", help="Mirror repo and spin up agent container",
                       parents=[container_flags])
    p.add_argument("github_url", metavar="github-url")
    p.set_defaults(func=cmd_create)

    p = sub.add_parser("attach", help="Attach to agent's tmux session")
    p.add_argument("project")
    p.set_defaults(func=cmd_attach)

    for name, help_text in [("stop", "Stop"), ("start", "Start"),
                            ("pause", "Freeze"), ("unpause", "Resume")]:
        p = sub.add_parser(name, help=f"{help_text} agent container(s)")
        p.add_argument("target", metavar="project|--all")
        p.set_defaults(func={"stop": cmd_stop, "start": cmd_start,
                              "pause": cmd_pause, "unpause": cmd_unpause}[name])

    p = sub.add_parser("sync", help="Trigger Gitea mirror sync")
    p.add_argument("project")
    p.set_defaults(func=cmd_sync)

    p = sub.add_parser("review", help="Fetch branch, show security review + safety checks")
    p.add_argument("project")
    p.add_argument("branch")
    p.set_defaults(func=cmd_review)

    p = sub.add_parser("recreate", help="New container + fresh token, keeps volume",
                       parents=[container_flags])
    p.add_argument("project")
    p.set_defaults(func=cmd_recreate)

    sub.add_parser("status", help="List all projects and containers").set_defaults(func=cmd_status)

    p = sub.add_parser("destroy", help="Remove container, volume, Gitea user")
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
