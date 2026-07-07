#!/usr/bin/env python3
"""sandbox.py — Main CLI for the LLM Agent Sandbox.

Thin argparse shims over cli/sandboxcore.py (the importable lifecycle core).
This file owns: argument parsing, interactive prompts, final result formatting,
os.execvp commands (attach/logs), and host-shaped commands (push/pull-context,
port, setup/unsetup, ci-watch, webui). Lifecycle logic lives in the core.
"""

import argparse
import base64
import fnmatch
import json
import os
import secrets
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError

import yaml

# Make the cli/ package importable regardless of how this script is invoked.
_REPO_ROOT = Path(__file__).resolve().parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Lifecycle core. Config, gitea_api_ok and http_basic_auth_request are unused
# here and imported ONLY as re-exports for ci-watch.py, which does
# `from sandbox import ...` — keep them until ci-watch is repointed.
from cli.sandboxcore import (  # noqa: E402
    SCRIPT_DIR,
    Config,
    CreateRequest,
    DestroyRequest,
    HarnessError,
    StartRequest,
    StopRequest,
    SyncRequest,
    ValidationError,
    build_agent_docker_args,
    container_exists,
    container_running,
    copy_container_files,
    create,
    create_workspace_volume,
    destroy,
    die,
    ensure_agent_network,
    find_free_port,
    gen_password,
    generate_gitea_token,
    get_agent_containers,
    gitea_api,
    gitea_api_ok,
    http_basic_auth_post,
    http_basic_auth_request,
    inject_route,
    install_agent,
    install_docker_dind,
    list_agents,
    load_config,
    profile_default_gpus,
    read_port_state,
    apply_port_state,
    remove_agent_network,
    remove_port_forwarder,
    remove_workspace_dir,
    resolve_profile_image,
    run,
    run_check,
    run_quiet,
    start,
    start_byobu_session,
    status,
    stop,
    sync,
    sysbox_available,
    wait_for_gitea,
    wire_webui_to_projects,
    _agent_ssh_info,
)

CONTEXT_CONFIG_FILE = SCRIPT_DIR / "context-config.yaml"
CONTEXT_CONFIG_EXAMPLE = SCRIPT_DIR / "context-config.yaml.example"


# ─── .env helpers ─────────────────────────────────────────────────────────────


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


def docker_compose(*args: str) -> None:
    run_check([
        "docker", "compose",
        "-f", str(SCRIPT_DIR / "docker-compose.yml"),
        "--env-file", str(SCRIPT_DIR / ".env"),
        *args,
    ])


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

    # CI Watch setup prompt
    if not cfg.ci_watch_enabled:
        answer = input("\nEnable CI watch for automated PR testing? [Y/n]: ").strip().lower()
        if answer not in ("n", "no"):
            _run_ci_watch("setup")
            # Reload config to pick up new CI watch settings
            cfg = load_config()

    # Start CI watch if enabled
    if cfg.ci_watch_enabled:
        _run_ci_watch("start")

    print(f"""
=== Setup Complete ===
Gitea UI:      http://localhost:{cfg.gitea_port}/explore/repos?sort=newest&type=fork
Gitea login:   sandbox-admin / {cfg.gitea_admin_password}
Projects dir:  {cfg.projects_dir}
CI watch:      {'enabled' if cfg.ci_watch_enabled else 'disabled (run sandbox ci-watch setup)'}
""")


def format_create_summary(res, cfg) -> str:
    """Render the post-create summary block (byte-identical to the pre-split
    output). The only place the SSH password is printed."""
    egress_label = "open (all ports)" if res.egress == "open" else "locked (80/443/DNS only)"
    docker_label = " (Docker-in-Docker)" if res.docker else ""
    branch_label = f"\nBase branch: {res.base_branch}" if res.base_branch else ""
    webui_import = _webui_import_string(res.project, res.ssh_password)
    return f"""
=== Sandbox ready: {res.project} ==={docker_label}
Attach:    sandbox attach {res.project}
SSH:       ssh agent@localhost -p {res.ssh_port}  (password: {res.ssh_password})
Gitea:     http://localhost:{cfg.gitea_port}/{res.gitea_user}/{res.project}
Gitea login: sandbox-admin / {cfg.gitea_admin_password}
Egress:    {egress_label}{branch_label}
WebUI import string (paste into the webui's Add project dialog):
  {webui_import}

To review agent work from your real repo:
  python fetch-sandbox.py {res.project} [<repo-path>] --branch <branch-name>
  python fetch-sandbox.py {res.project} [<repo-path>] --pr <pr-number>
  python fetch-sandbox.py {res.project} [<repo-path>] --commit <sha>"""


def cmd_create(args: argparse.Namespace) -> None:
    cfg = load_config()
    req = CreateRequest.from_kwargs(
        github_url=args.github_url, branch=args.branch,
        egress="open" if args.open_egress else "",
        memory=args.memory, cpus=args.cpus, gpus=args.gpus,
        profile=args.profile, ssh_port=args.ssh_port,
        agent=args.agent, docker=args.docker,
    )
    res = create(req, cfg)
    print(format_create_summary(res, cfg))


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
    target = resolve_target(args)
    if target == "--all":
        containers = get_agent_containers()
        if not containers:
            print("No sandbox agent containers found.")
            return
        for name in containers:
            print(f"stop {name}...")
            stop(StopRequest.from_kwargs(project=name.removeprefix("sandbox-agent-")))
    else:
        stop(StopRequest.from_kwargs(project=target))


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
            start(StartRequest.from_kwargs(project=name.removeprefix("sandbox-agent-")), cfg)
    else:
        start(StartRequest.from_kwargs(project=target), cfg)


def cmd_pause(args: argparse.Namespace) -> None:
    for_containers("pause", resolve_target(args))


def cmd_unpause(args: argparse.Namespace) -> None:
    for_containers("unpause", resolve_target(args))


# ─── Context sync (push-context / pull-context) ───────────────────────────────


def load_context_config() -> dict:
    """Load context-config.yaml (or .example fallback). Die if neither exists."""
    if CONTEXT_CONFIG_FILE.exists():
        path = CONTEXT_CONFIG_FILE
    elif CONTEXT_CONFIG_EXAMPLE.exists():
        path = CONTEXT_CONFIG_EXAMPLE
    else:
        die(f"context-config.yaml not found. Expected at {CONTEXT_CONFIG_FILE} "
            f"or {CONTEXT_CONFIG_EXAMPLE}.")
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as e:
        die(f"Failed to parse {path}: {e}")
    if not isinstance(data, dict):
        die(f"{path}: top-level value must be a mapping")
    for key in ("push", "pull", "exclude"):
        val = data.get(key, [])
        if val is None:
            val = []
        if not isinstance(val, list):
            die(f"{path}: '{key}' must be a list")
        for item in val:
            if not isinstance(item, str):
                die(f"{path}: '{key}' entries must be strings (got {item!r})")
        data[key] = val
    return data


def _resolve_context_paths(direction: str, cli_include: list[str],
                            cli_exclude: list[str], cfg: dict) -> tuple[list[str], list[str]]:
    """Merge config + CLI lists. CLI includes override matching config excludes."""
    includes = list(cfg.get(direction, [])) + list(cli_include or [])
    cfg_excl = [e for e in cfg.get("exclude", []) if e not in (cli_include or [])]
    excludes = cfg_excl + list(cli_exclude or [])
    return includes, excludes


def _matches_exclude(rel_path: str, excludes: list[str]) -> bool:
    """fnmatch the rel path and basename; '/'-suffixed entries match any directory component."""
    parts = rel_path.split("/")
    basename = parts[-1]
    for pat in excludes:
        if pat.endswith("/"):
            if pat.rstrip("/") in parts:
                return True
        else:
            if fnmatch.fnmatch(rel_path, pat) or fnmatch.fnmatch(basename, pat):
                return True
    return False


def _plan_context_copies(src_root: Path, dst_root: Path, includes: list[str],
                          excludes: list[str]) -> tuple[list[tuple[Path, Path, str]], int, int]:
    """Walk src_root for each include, return (plan, skipped_symlinks, skipped_excludes).

    plan items are (src_file, dst_file, rel_path). Symlinks (file or dir) are skipped.
    """
    plan: list[tuple[Path, Path, str]] = []
    skipped_symlinks = 0
    skipped_excludes = 0
    seen: set[str] = set()

    for entry in includes:
        raw = src_root / entry
        if not raw.exists() and not raw.is_symlink():
            continue
        if raw.is_symlink():
            skipped_symlinks += 1
            continue
        if raw.is_file():
            rel = raw.relative_to(src_root).as_posix()
            if _matches_exclude(rel, excludes):
                skipped_excludes += 1
                continue
            if rel in seen:
                continue
            seen.add(rel)
            plan.append((raw, dst_root / rel, rel))
            continue
        if raw.is_dir():
            for root, dirs, files in os.walk(raw, followlinks=False):
                root_path = Path(root)
                for d in list(dirs):
                    dp = root_path / d
                    rel_d = dp.relative_to(src_root).as_posix()
                    if dp.is_symlink():
                        skipped_symlinks += 1
                        dirs.remove(d)
                        continue
                    if _matches_exclude(rel_d, excludes):
                        skipped_excludes += 1
                        dirs.remove(d)
                for f in files:
                    fp = root_path / f
                    if fp.is_symlink():
                        skipped_symlinks += 1
                        continue
                    rel = fp.relative_to(src_root).as_posix()
                    if _matches_exclude(rel, excludes):
                        skipped_excludes += 1
                        continue
                    if rel in seen:
                        continue
                    seen.add(rel)
                    plan.append((fp, dst_root / rel, rel))
    return plan, skipped_symlinks, skipped_excludes


def _apply_context_copies(plan: list[tuple[Path, Path, str]], dry_run: bool) -> int:
    """Perform the copies (or measure them for --dry-run). Returns bytes touched."""
    total_bytes = 0
    for src, dst, _rel in plan:
        try:
            total_bytes += src.stat().st_size
        except OSError:
            pass
        if dry_run:
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    return total_bytes


def _project_workspace(cfg: Config, project: str) -> Path:
    """Host path to the agent's clone of the project repo (bind-mounted to /home/agent/<project>)."""
    if not cfg.projects_dir:
        die("PROJECTS_DIR is not set in .env.")
    return Path(cfg.projects_dir) / project / project


def cmd_push_context(args: argparse.Namespace) -> None:
    cfg_env = load_config()
    cfg = load_context_config()
    project = args.project
    workspace = _project_workspace(cfg_env, project)
    if not workspace.is_dir():
        die(f"Workspace not found at {workspace}. Run: sandbox.py create <github-url>")

    host_root = Path.cwd()
    includes, excludes = _resolve_context_paths("push", args.include, args.exclude, cfg)
    plan, skipped_sym, skipped_exc = _plan_context_copies(host_root, workspace, includes, excludes)

    if not plan:
        print(f"Nothing to push from {host_root} → {workspace}")
        if skipped_sym or skipped_exc:
            print(f"Skipped: {skipped_sym} symlinks, {skipped_exc} excluded")
        return

    existing = [(s, d, r) for s, d, r in plan if d.exists()]

    print(f"{'Would copy' if args.dry_run else 'Copying'} {len(plan)} file(s) "
          f"from {host_root} → {workspace}:")
    for _src, dst, rel in plan:
        marker = " (overwrite)" if dst.exists() else ""
        print(f"  {rel}{marker}")

    bytes_touched = _apply_context_copies(plan, args.dry_run)
    verb = "Would copy" if args.dry_run else "Copied"
    print(f"\n{verb} {len(plan)} files ({bytes_touched} bytes).")
    if existing:
        overwrite_verb = "Would overwrite" if args.dry_run else "Overwrote"
        print(f"{overwrite_verb} {len(existing)} existing file(s).")
    if skipped_sym or skipped_exc:
        print(f"Skipped: {skipped_sym} symlinks, {skipped_exc} excluded")


def cmd_pull_context(args: argparse.Namespace) -> None:
    cfg_env = load_config()
    cfg = load_context_config()
    project = args.project
    workspace = _project_workspace(cfg_env, project)
    if not workspace.is_dir():
        die(f"Workspace not found at {workspace}. Run: sandbox.py create <github-url>")

    host_root = Path.cwd()
    includes, excludes = _resolve_context_paths("pull", args.include, args.exclude, cfg)
    plan, skipped_sym, skipped_exc = _plan_context_copies(workspace, host_root, includes, excludes)

    existing = [(s, d, r) for s, d, r in plan if d.exists()]
    blocked = bool(existing) and not args.overwrite

    if not plan:
        print(f"Nothing to pull from {workspace} → {host_root}")
        if skipped_sym or skipped_exc:
            print(f"Skipped: {skipped_sym} symlinks, {skipped_exc} excluded")
        return

    if blocked:
        print("Cannot pull: the following host files already exist:")
        for _s, _d, r in existing:
            print(f"  {r}")
        print()

    print(f"{'Would copy' if args.dry_run else 'Copying'} {len(plan)} file(s) "
          f"from {workspace} → {host_root}:")
    for _src, dst, rel in plan:
        marker = " (overwrite)" if dst.exists() else ""
        print(f"  {rel}{marker}")

    if blocked:
        print("\nRe-run with --overwrite to proceed.")
        sys.exit(1)

    bytes_touched = _apply_context_copies(plan, args.dry_run)
    verb = "Would copy" if args.dry_run else "Copied"
    print(f"\n{verb} {len(plan)} files ({bytes_touched} bytes).")
    if args.overwrite and existing:
        overwrite_verb = "Would overwrite" if args.dry_run else "Overwrote"
        print(f"{overwrite_verb} {len(existing)} existing file(s).")
    if skipped_sym or skipped_exc:
        print(f"Skipped: {skipped_sym} symlinks, {skipped_exc} excluded")


def cmd_sync(args: argparse.Namespace) -> None:
    cfg = load_config()
    sync(SyncRequest.from_kwargs(project=args.project), cfg)
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
    # Check if CI_WATCH_ENABLED is set in the container
    r = run(["docker", "exec", container_name, "bash", "-c",
             "echo ${CI_WATCH_ENABLED:-}"], capture_output=True, text=True)
    ci_watch_in_container = r.stdout.strip() == "true"
    # Discover template files dynamically (agent-specific + universal .md files)
    r = run(["docker", "exec", container_name, "bash", "-c",
             r"for f in /home/agent/.*.template; do [ -f \"$f\" ] && basename \"$f\" .template | sed 's/^\\.//'; done"],
            capture_output=True, text=True)
    template_files = [f.strip() for f in r.stdout.strip().split('\n') if f.strip()]
    for filename in template_files:
        template = f"{home}/.{filename}.template"
        target = f"{home}/{filename}"
        # Re-render {{BASE_BRANCH}}
        render_cmd = (
            f"cp '{template}' '{target}' && "
            f"sed -i 's/{{{{BASE_BRANCH}}}}/{branch}/g' '{target}'"
        )
        run(["docker", "exec", container_name, "bash", "-c", render_cmd],
            capture_output=True)
        # Re-render {{#CI_WATCH}} / {{^CI_WATCH}} conditionals
        if ci_watch_in_container:
            ci_cmd = (
                r"sed -i '/{{^CI_WATCH}}/,/{{\/CI_WATCH}}/d' '" + target + "' && "
                r"sed -i '/{{#CI_WATCH}}/d; /{{\/CI_WATCH}}/d' '" + target + "'"
            )
        else:
            ci_cmd = (
                r"sed -i '/{{#CI_WATCH}}/,/{{\/CI_WATCH}}/d' '" + target + "' && "
                r"sed -i '/{{^CI_WATCH}}/d; /{{\/CI_WATCH}}/d' '" + target + "'"
            )
        run(["docker", "exec", container_name, "bash", "-c", ci_cmd],
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
    if args.agent and not (SCRIPT_DIR / "container" / args.agent).is_dir():
        die(f"Unknown agent '{args.agent}'. Available: {', '.join(list_agents())}")
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
            remove_workspace_dir(cfg, project)

    # Recreate volume
    print("Creating fresh volume...")
    create_workspace_volume(cfg, project, volume_name)

    # Copy container/ files to agent home and fix ownership (two-layer: universal + agent)
    copy_container_files(volume_name, args.agent)

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
        gpus=args.gpus or profile_default_gpus(profile), agent_type=args.agent,
        docker=args.docker, ci_watch=cfg.ci_watch_enabled,
    )

    print("Starting new container...")
    run_check(["docker", *docker_args])

    # Inject default route through the router
    print("Injecting network route...")
    inject_route(container_name, router_ip)

    # Install agent CLI if --agent (needs network, so after route injection)
    if args.agent:
        install_agent(args.agent, container_name)

    # Install Docker-in-Docker if --docker (needs network, so after route injection)
    if args.docker:
        install_docker_dind(container_name)

    # Start byobu session (after all installs so shell inherits final environment)
    start_byobu_session(container_name)

    print(f"""
=== Recreated: {project} ===
Attach:  sandbox attach {project}
SSH:     ssh agent@localhost -p {ssh_port}  (password: {ssh_pass})""")


def format_status(res, cfg, ci_pid) -> str:
    """Render the status report (byte-identical to the pre-split output)."""
    lines = ["=== Sandbox Status ===\n"]

    lines.append("── Infrastructure ──")
    for svc in res.infra:
        lines.append(f"  {svc.name:<20s} {svc.state}")
    if cfg.gitea_admin_password:
        lines.append(f"\n  Gitea UI:    http://localhost:{cfg.gitea_port}")
        lines.append(f"  Gitea login: sandbox-admin / {cfg.gitea_admin_password}")

    # CI Watch status
    lines.append("\n── CI Watch ──")
    if ci_pid:
        lines.append(f"  Status:    running (PID {ci_pid})")
        lines.append(f"  Poll:      every {cfg.ci_watch_poll_interval}s")
        lines.append(f"  Log:       {CI_WATCH_LOG_FILE}")
        lines.append("  Commands:  /test-pr, /test-pr-bug")
    elif cfg.ci_watch_enabled:
        lines.append("  Status:    configured but not running")
        lines.append("  Run 'sandbox ci-watch start' or 'sandbox up' to start.")
    else:
        lines.append("  Status:    not configured")
        lines.append("  Run 'sandbox ci-watch setup' to enable.")

    # Active CI test containers
    if res.ci_test_lines:
        lines.append("\n── Active CI Tests ──")
        for line in res.ci_test_lines:
            parts = line.split("\t")
            name = parts[0] if parts else "?"
            state = parts[1] if len(parts) > 1 else "?"
            pr = parts[2] if len(parts) > 2 else ""
            lines.append(f"  {name:<30s} {state:<20s} {pr}")

    lines.append("\n── Agent Containers ──")
    if not res.agents:
        lines.append("  (no agent containers)")
    else:
        # Collect data first to compute column widths
        rows = []
        for a in res.agents:
            ports = (f"{a.port_bind} -> {','.join(str(h) for h, _ in a.port_mappings)}"
                     if a.port_mappings else "-")
            rows.append((a.project, a.state, a.ssh_port, ports))
        name_width = max(len(r[0]) for r in rows) + 4
        name_width = max(name_width, len("PROJECT"))
        ports_width = max((len(r[3]) for r in rows), default=0)
        ports_width = max(ports_width, len("PORTS"))
        lines.append(f"  {'PROJECT':<{name_width}s}  {'STATE':<10s}  {'SSH PORT':<10s}  {'PORTS':<{ports_width}s}")
        for project_name, state, ssh_port, ports in rows:
            lines.append(f"  {project_name:<{name_width}s}  {state:<10s}  {ssh_port:<10s}  {ports:<{ports_width}s}")

    if res.projects_dir:
        lines.append(f"\n── Projects Directory ──\n  {res.projects_dir}")
        for name in res.project_dirs:
            lines.append(f"    {name}/")
    else:
        lines.append("\n── Projects Directory ──\n  (standard Docker volumes)")

    return "\n".join(lines)


def cmd_status(args: argparse.Namespace) -> None:
    cfg = load_config()
    res = status(cfg)
    print(format_status(res, cfg, _ci_watch_pid()))


def cmd_port(args: argparse.Namespace) -> None:
    project = args.project
    agent = f"sandbox-agent-{project}"
    network = f"sandbox-net-{project}"
    cur_bind, mappings = read_port_state(project)

    if args.list:
        if not mappings:
            print(f"No open ports for {project}.")
            return
        print(f"  bind: {cur_bind}")
        print(f"  {'HOST':<8s}  CONTAINER")
        for h, c in mappings:
            print(f"  {h:<8d}  {c}")
        return

    if args.stop:
        if not mappings:
            print(f"No port forwarder running for {project}.")
            return
        remove_port_forwarder(project)
        print(f"Stopped port forwarder for {project}.")
        return

    if args.close is not None:
        new_mappings = [(h, c) for h, c in mappings if h != args.close]
        if len(new_mappings) == len(mappings):
            die(f"Port {args.close} not open on {project}.")
        apply_port_state(project, cur_bind, new_mappings)
        if new_mappings:
            print(f"Closed {args.close} on {project}.")
        else:
            print(f"Closed {args.close}; no ports remaining, forwarder stopped.")
        return

    # --open
    try:
        host_str, cont_str = args.open.split(":", 1)
        host_port, cont_port = int(host_str), int(cont_str)
    except ValueError:
        die("--open expects HOST:CONTAINER (e.g. 8080:8080).")

    if not container_exists(agent):
        die(f"Container {agent} not found.")
    if not run_quiet(["docker", "network", "inspect", network]):
        die(f"Network {network} not found.")
    if any(h == host_port for h, _ in mappings):
        die(f"Host port {host_port} already mapped on {project}.")

    if mappings:
        if args.bind and args.bind != cur_bind:
            die(f"Existing forwarder for {project} binds to {cur_bind}; "
                f"--close all ports first to rebind.")
        bind = cur_bind
    else:
        bind = args.bind or "127.0.0.1"

    apply_port_state(project, bind, mappings + [(host_port, cont_port)])
    print(f"Opened http://{bind}:{host_port} -> {agent}:{cont_port}")


def cmd_destroy(args: argparse.Namespace) -> None:
    cfg = load_config()
    req = DestroyRequest.from_kwargs(project=args.project)
    project = req.project

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

    destroy(req, cfg)

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

    # 0. Stop CI watch
    _run_ci_watch("stop")

    # 1. Destroy all agent containers, volumes, networks, and Gitea users
    if containers:
        print("── Destroying all agent projects ──")
        for name in containers:
            project = name.removeprefix("sandbox-agent-")
            volume_name = f"sandbox-{project}"

            print(f"  Removing {name}...")
            run(["docker", "rm", "-f", name], capture_output=True)

            if run_quiet(["docker", "volume", "inspect", volume_name]):
                run(["docker", "volume", "rm", volume_name], capture_output=True)

            if cfg.projects_dir and (Path(cfg.projects_dir) / project).is_dir():
                remove_workspace_dir(cfg, project)

            remove_agent_network(project)
    else:
        print("No agent containers found.")

    # 2. Stop and remove infrastructure containers + volumes
    print("\n── Removing infrastructure ──")
    docker_compose("down", "-v")

    # 4. Remove generated tokens from .env
    env_file = SCRIPT_DIR / ".env"
    cleanup_prefixes = ("GITEA_ADMIN_TOKEN=", "GITEA_ADMIN_PASSWORD=", "GITEA_SECRET_KEY=",
                        "CI_WATCH_ENABLED=", "CI_WATCH_POLL_INTERVAL=", "CI_WATCH_GITEA_TOKEN=")
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


# ─── CI Watch (delegated to ci-watch.py) ─────────────────────────────────────

CI_WATCH_DIR = SCRIPT_DIR / ".ci-watch"
CI_WATCH_PID_FILE = CI_WATCH_DIR / "ci-watch.pid"
CI_WATCH_LOG_FILE = CI_WATCH_DIR / "ci-watch.log"
CI_CONFIG_FILE = SCRIPT_DIR / "ci-config.yaml"


def _run_ci_watch(*args: str) -> None:
    """Run ci-watch.py with the given arguments. Pure passthrough."""
    script = SCRIPT_DIR / "ci-watch.py"
    if not script.exists():
        die("ci-watch.py not found.")
    r = subprocess.run([sys.executable, str(script), *args])
    if r.returncode != 0:
        sys.exit(r.returncode)


def _ci_watch_pid() -> int | None:
    """Read the CI watch PID file. Returns PID or None if not running."""
    if not CI_WATCH_PID_FILE.exists():
        return None
    try:
        pid = int(CI_WATCH_PID_FILE.read_text().strip())
        os.kill(pid, 0)  # Check if process exists
        return pid
    except (ValueError, ProcessLookupError, PermissionError):
        CI_WATCH_PID_FILE.unlink(missing_ok=True)
        return None


def cmd_ci_watch(args: argparse.Namespace) -> None:
    """Route ci-watch subcommands to ci-watch.py."""
    _run_ci_watch(args.ci_watch_action)


# ── sandbox up / down ──

def cmd_up(args: argparse.Namespace) -> None:
    """Start infrastructure and CI watch (if configured)."""
    cfg = load_config()

    print("Starting infrastructure...")
    docker_compose("up", "-d", "gitea", "router")
    wait_for_gitea(cfg)
    print("Infrastructure is up.")

    if cfg.ci_watch_enabled:
        _run_ci_watch("start")


def cmd_down(args: argparse.Namespace) -> None:
    """Stop infrastructure, CI watch, and webui."""
    _run_ci_watch("stop")

    if container_exists("sandbox-webui"):
        print("Stopping webui...")
        run(["docker", "rm", "-f", "sandbox-webui"], capture_output=True)

    print("Stopping infrastructure...")
    docker_compose("down")
    print("Infrastructure is down.")


def _webui_import_string(project: str, ssh_pass: str) -> str:
    """Build the base64 import string for the webui.

    Uses the agent container's name on its per-project network and the
    internal SSH port (22), so the webui SSHes via the project net rather
    than via the host's published port.
    """
    return base64.b64encode(json.dumps({
        "name": project,
        "host": f"sandbox-agent-{project}",
        "port": 22,
        "username": "agent",
        "password": ssh_pass,
    }).encode()).decode()


def cmd_webui(args: argparse.Namespace) -> None:
    """Manage the optional webui container (start / stop / status / import)."""
    action = args.webui_action
    port = read_env_value("WEBUI_PORT") or "7777"
    bind = read_env_value("WEBUI_BIND") or "127.0.0.1"

    if action == "import":
        target = getattr(args, "project", None)
        if target:
            info = _agent_ssh_info(target)
            if not info:
                die(f"Agent {target} not found or missing SSH info.")
            _, ssh_pass = info
            print(_webui_import_string(target, ssh_pass))
            return
        containers = get_agent_containers()
        if not containers:
            print("No agent containers found.")
            return
        rows = []
        for c in containers:
            p = c.removeprefix("sandbox-agent-")
            info = _agent_ssh_info(p)
            if info:
                rows.append((p, _webui_import_string(p, info[1])))
        if not rows:
            print("No agent containers with SSH info available.")
            return
        name_w = max(len(p) for p, _ in rows)
        for p, s in rows:
            print(f"{p.ljust(name_w)}  {s}")
        return

    if action == "start":
        new_bind = getattr(args, "bind", None)
        if new_bind and new_bind != bind:
            update_env_key("WEBUI_BIND", new_bind)
            bind = new_bind
            if container_running("sandbox-webui"):
                print(f"Bind changed to {new_bind}; restarting webui...")
                run(["docker", "rm", "-f", "sandbox-webui"], capture_output=True)
        if container_running("sandbox-webui"):
            wire_webui_to_projects()
            print(f"WebUI already running at https://{bind}:{port}")
            return
        if container_exists("sandbox-webui"):
            run(["docker", "rm", "-f", "sandbox-webui"], capture_output=True)
        if not run_quiet(["docker", "image", "inspect", "sandbox-webui:latest"]):
            print("Building webui image...")
            docker_compose("--profile", "webui", "build", "webui")
        print(f"Starting webui (bind {bind}:{port})...")
        docker_compose("--profile", "webui", "up", "-d", "webui")
        wire_webui_to_projects()
        print(f"WebUI:  https://{bind}:{port}")
        print(f"  (self-signed cert — your browser will warn on first visit; click through)")
        return

    if action == "stop":
        if not container_exists("sandbox-webui"):
            print("WebUI not running.")
            return
        print("Stopping webui...")
        run(["docker", "rm", "-f", "sandbox-webui"], capture_output=True)
        print("WebUI stopped.")
        return

    if action == "status":
        if container_running("sandbox-webui"):
            print(f"WebUI: running")
            print(f"  https://{bind}:{port}")
        elif container_exists("sandbox-webui"):
            print("WebUI: stopped (container exists)")
        else:
            print(f"WebUI: not running  (configured bind {bind}:{port})")
        return


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
    container_flags.add_argument("--agent", default="",
                                 help="Agent to install and configure (e.g. claude, opencode)")
    container_flags.add_argument("--docker", action="store_true",
                                 help="Enable Docker-in-Docker via Sysbox runtime")

    sub.add_parser("setup", help="One-time infrastructure setup").set_defaults(func=cmd_setup)
    sub.add_parser("unsetup", help="Tear down all infrastructure, containers, and volumes").set_defaults(func=cmd_unsetup)

    sub.add_parser("up", help="Start infrastructure + CI watch").set_defaults(func=cmd_up)
    sub.add_parser("down", help="Stop infrastructure + CI watch").set_defaults(func=cmd_down)

    # CI Watch subcommands
    ci_watch = sub.add_parser("ci-watch", help="Manage CI watch background process")
    ci_watch_sub = ci_watch.add_subparsers(dest="ci_watch_action", required=True)
    ci_watch_sub.add_parser("setup", help="Configure CI watch")
    ci_watch_sub.add_parser("start", help="Start CI watch")
    ci_watch_sub.add_parser("stop", help="Stop CI watch")
    ci_watch.set_defaults(func=cmd_ci_watch)

    # WebUI subcommands
    webui = sub.add_parser("webui", help="Manage the optional webui container")
    webui_sub = webui.add_subparsers(dest="webui_action", required=True)
    webui_start = webui_sub.add_parser("start", help="Build (if needed) and start the webui")
    webui_start.add_argument("--bind", metavar="IP",
                             help="Host IP to bind on (default: 127.0.0.1; persisted to .env as WEBUI_BIND)")
    webui_sub.add_parser("stop", help="Stop the webui")
    webui_sub.add_parser("status", help="Show webui status and URL")
    webui_import = webui_sub.add_parser("import",
        help="Print webui import string(s); no arg = all projects")
    webui_import.add_argument("project", nargs="?",
        help="project name (omit to list every agent container)")
    webui.set_defaults(func=cmd_webui)

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

    for ctx_name, ctx_help, ctx_func in [
        ("push-context", "Copy memory/plan/agent-config files from host repo (cwd) to agent workspace", cmd_push_context),
        ("pull-context", "Copy memory/plan/agent-config files from agent workspace to host repo (cwd)", cmd_pull_context),
    ]:
        p = sub.add_parser(ctx_name, help=ctx_help)
        p.add_argument("project")
        p.add_argument("--include", action="append", default=[], metavar="PATH",
                       help="Extra path to copy (repeatable); also cancels matching config exclude entries")
        p.add_argument("--exclude", action="append", default=[], metavar="PATH",
                       help="Extra glob to exclude (repeatable)")
        p.add_argument("--dry-run", action="store_true", help="Show what would be copied; write nothing")
        if ctx_name == "pull-context":
            p.add_argument("--overwrite", action="store_true",
                           help="Allow overwriting existing host files")
        p.set_defaults(func=ctx_func)

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

    p = sub.add_parser("port", help="Manage the project's port forwarder")
    p.add_argument("project")
    p.add_argument("--bind", default=None, metavar="IP",
                   help="Host IP to bind on (default: 127.0.0.1; only honored when starting a fresh forwarder)")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--open", metavar="HOST:CONTAINER",
                   help="Add a HOST:CONTAINER mapping (restarts forwarder)")
    g.add_argument("--close", metavar="HOST", type=int,
                   help="Remove a HOST mapping (restarts forwarder, or stops it if last)")
    g.add_argument("--list", action="store_true",
                   help="List current mappings for this project")
    g.add_argument("--stop", action="store_true",
                   help="Stop the forwarder, removing all mappings")
    p.set_defaults(func=cmd_port)

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
    except ValidationError as e:
        die(str(e))
    except HarnessError as e:
        die(e.client_detail or e.log_msg)
    except RuntimeError as e:
        die(str(e))


if __name__ == "__main__":
    main()
