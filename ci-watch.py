#!/usr/bin/env python3
"""ci-watch.py — CI watch background process for the LLM Agent Sandbox.

Polls Gitea PRs for CI commands (/test-pr, /test-pr-bug) and runs tests
in isolated Docker containers (--network=none, --cap-drop=ALL).

Usage:
    python3 ci-watch.py setup   # Interactive configuration
    python3 ci-watch.py start   # Start background polling loop
    python3 ci-watch.py stop    # Stop background process

Normally invoked via 'sandbox ci-watch {setup|start|stop}' or started
automatically by 'sandbox up' when CI watch is enabled.
"""

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from urllib.request import Request, urlopen

from sandbox import (
    SCRIPT_DIR,
    Config,
    die,
    gen_password,
    gitea_api,
    gitea_api_ok,
    http_basic_auth_request,
    load_config,
    update_env_key,
)

# ─── Constants ────────────────────────────────────────────────────────────────

CI_LOGS_DIR = SCRIPT_DIR / "ci-logs"
CI_WATCH_PID_FILE = SCRIPT_DIR / "ci-watch.pid"
CI_WATCH_LOG_FILE = SCRIPT_DIR / "ci-watch.log"
CI_COMMANDS_FILE = SCRIPT_DIR / "ci-commands.json"

# Validation patterns
_BRANCH_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9/_.-]*$")
_FILE_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9/_.-]*\.(py|sh)$")


# ─── CI command parsing ──────────────────────────────────────────────────────


def _load_ci_commands() -> dict:
    """Load CI command definitions from ci-commands.json."""
    if not CI_COMMANDS_FILE.exists():
        die(f"ci-commands.json not found at {CI_COMMANDS_FILE}")
    return json.loads(CI_COMMANDS_FILE.read_text())


def _build_ci_command_patterns(ci_config: dict) -> list[tuple[str, re.Pattern, dict]]:
    """Build regex patterns from ci-commands.json for matching PR comments.

    Returns list of (command_name, regex_pattern, command_config).
    """
    patterns = []
    for cmd_name, cmd_cfg in ci_config.get("commands", {}).items():
        cmd_type = cmd_cfg.get("type", "")
        if cmd_type == "bug-verification":
            # /test-pr-bug <test-file> <branch>
            pat = re.compile(rf"^{re.escape(cmd_name)}\s+(\S+)\s+(\S+)\s*$", re.MULTILINE)
        elif cmd_type == "command":
            # /test-pr "<command>" <branch>
            pat = re.compile(rf'^{re.escape(cmd_name)}\s+"([^"]+)"\s+(\S+)\s*$', re.MULTILINE)
        elif cmd_type == "fixed-command":
            # /lint <branch>
            pat = re.compile(rf"^{re.escape(cmd_name)}\s+(\S+)\s*$", re.MULTILINE)
        else:
            continue
        patterns.append((cmd_name, pat, cmd_cfg))
    return patterns


def _parse_ci_command(comment_body: str, patterns: list) -> tuple[str, dict, dict] | None:
    """Parse a CI command from a PR comment.

    Returns (command_name, parsed_args, command_config) or None.
    """
    for cmd_name, pat, cmd_cfg in patterns:
        m = pat.search(comment_body)
        if not m:
            continue
        cmd_type = cmd_cfg.get("type", "")
        if cmd_type == "bug-verification":
            return cmd_name, {"test_file": m.group(1), "branch": m.group(2)}, cmd_cfg
        elif cmd_type == "command":
            return cmd_name, {"test_command": m.group(1), "branch": m.group(2)}, cmd_cfg
        elif cmd_type == "fixed-command":
            return cmd_name, {"branch": m.group(1)}, cmd_cfg
    return None


def _validate_ci_args(command: str, args: dict, cmd_cfg: dict,
                      cfg: Config, repo_path: str) -> str | None:
    """Validate CI command arguments. Returns error message or None."""
    branch = args.get("branch", "")
    arg_defs = cmd_cfg.get("args", {})

    # Validate branch
    branch_pat = arg_defs.get("branch", {}).get("pattern", r"^[a-zA-Z0-9][a-zA-Z0-9/_.-]*$")
    if not re.match(branch_pat, branch):
        return f"Invalid branch name: `{branch}`"
    if not gitea_api_ok(cfg, "GET", f"/repos/{repo_path}/branches/{branch}"):
        return f"Branch `{branch}` does not exist in {repo_path}"

    if "test_file" in args:
        test_file = args["test_file"]
        file_pat = arg_defs.get("test_file", {}).get("pattern", r"^[a-zA-Z0-9][a-zA-Z0-9/_.-]*\.(py|sh)$")
        if not re.match(file_pat, test_file):
            return f"Invalid test file path: `{test_file}`"
        if ".." in test_file:
            return f"Path traversal not allowed: `{test_file}`"
        if not gitea_api_ok(cfg, "GET", f"/repos/{repo_path}/contents/{test_file}?ref={branch}"):
            return f"File `{test_file}` not found on branch `{branch}`"

    return None


# ─── Rate limiting ────────────────────────────────────────────────────────────

_rate_log: dict[str, list[float]] = {}


def _check_rate_limit(pr_key: str, limit: int = 10, window: int = 3600) -> bool:
    """Check per-PR rate limit. Returns True if allowed."""
    now = time.time()
    _rate_log.setdefault(pr_key, [])
    _rate_log[pr_key] = [t for t in _rate_log[pr_key] if now - t < window]
    if len(_rate_log[pr_key]) >= limit:
        return False
    _rate_log[pr_key].append(now)
    return True


# ─── Test execution ──────────────────────────────────────────────────────────


def _get_agent_container_info(project: str) -> dict:
    """Inspect the agent container for image and runtime config.

    Returns a dict with 'image' and 'runtime' (e.g. 'runc' or 'sysbox-runc').
    """
    info = {"image": "sandbox-agent-python:latest", "runtime": "runc"}
    container = f"sandbox-agent-{project}"
    try:
        r = subprocess.run(
            ["docker", "inspect",
             "--format", "{{.Config.Image}}\t{{.HostConfig.Runtime}}",
             container],
            capture_output=True, text=True, timeout=10)
        if r.returncode == 0 and r.stdout.strip():
            parts = r.stdout.strip().split("\t")
            if parts[0]:
                info["image"] = parts[0]
            if len(parts) > 1 and parts[1]:
                info["runtime"] = parts[1]
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return info


# Capabilities matching the agent container (see sandbox.py build_docker_run_args)
_AGENT_CAPS = [
    "--cap-drop=ALL",
    "--cap-add=CHOWN", "--cap-add=DAC_OVERRIDE", "--cap-add=FOWNER",
    "--cap-add=SETGID", "--cap-add=SETUID", "--cap-add=KILL",
    "--cap-add=FSETID", "--cap-add=AUDIT_WRITE", "--cap-add=NET_RAW",
]


def _build_test_docker_args(repo_dir: str, image: str, runtime: str,
                            memory: str, cpus: str, pr_label: str,
                            script: str) -> list[str]:
    """Build docker run args for a CI test container.

    Mirrors the agent container's image, capabilities, and runtime,
    but with --network=none, --entrypoint="" (skip agent setup),
    and no credentials.
    """
    args = ["docker", "run", "--rm",
            f"--memory={memory}", f"--cpus={cpus}",
            "--label", "sandbox.ci-test=true",
            "--label", f"sandbox.ci-pr={pr_label}",
            "-v", f"{repo_dir}:/repo"]

    if runtime == "sysbox-runc":
        # Sysbox: capabilities are namespace-scoped, no manual cap config
        args += [f"--runtime={runtime}", "--pids-limit=2048"]
    else:
        args += _AGENT_CAPS + ["--pids-limit=512"]

    args += ["--entrypoint", "", image, "bash", "-c", script]
    return args


def _run_bug_verification(repo_dir: str, pr_branch: str, base_branch: str,
                          test_file: str, image: str, runtime: str,
                          ci_defaults: dict, pr_label: str) -> dict:
    """Run time-travel bug verification. Returns result dict."""
    ext = Path(test_file).suffix
    runner = "python3" if ext == ".py" else "bash"
    timeout = ci_defaults.get("timeout", 600)
    memory = ci_defaults.get("memory", "2g")
    cpus = ci_defaults.get("cpus", "2")

    script = f"""
set -e
cd /repo

git checkout --quiet '{pr_branch}' 2>&1
COMMIT=$(git rev-parse --short HEAD)

# Save test file from PR branch
mkdir -p /tmp/saved
cp '{test_file}' /tmp/saved/repro

# === Step 1: Base branch (expect FAIL) ===
git checkout --quiet '{base_branch}' 2>&1
mkdir -p "$(dirname '{test_file}')"
cp /tmp/saved/repro '{test_file}'
echo '=== Running on base branch ({base_branch}) ==='
set +e
{runner} '{test_file}' 2>&1
BASE_RC=$?
set -e
echo "=== Base exit code: $BASE_RC ==="

# === Step 2: PR branch (expect PASS) ===
git checkout --quiet '{pr_branch}' 2>&1
echo '=== Running on PR branch ({pr_branch}) ==='
set +e
{runner} '{test_file}' 2>&1
PR_RC=$?
set -e
echo "=== PR exit code: $PR_RC ==="

# Structured output
echo '---RESULTS---'
echo "COMMIT=$COMMIT"
echo "BASE_RC=$BASE_RC"
echo "PR_RC=$PR_RC"
"""

    try:
        r = subprocess.run(
            _build_test_docker_args(repo_dir, image, runtime, memory, cpus,
                                    pr_label, script),
            capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"success": False, "output": "Verification timed out.",
                "commit": "", "base_rc": -1, "pr_rc": -1}

    output = r.stdout + r.stderr
    result: dict = {"output": output, "commit": "", "base_rc": -1, "pr_rc": -1}

    if "---RESULTS---" in output:
        for line in output.split("---RESULTS---")[1].splitlines():
            line = line.strip()
            if line.startswith("COMMIT="):
                result["commit"] = line.split("=", 1)[1]
            elif line.startswith("BASE_RC="):
                result["base_rc"] = int(line.split("=", 1)[1])
            elif line.startswith("PR_RC="):
                result["pr_rc"] = int(line.split("=", 1)[1])

    result["success"] = (result["base_rc"] != 0 and result["pr_rc"] == 0)
    return result


def _run_test_verification(repo_dir: str, pr_branch: str, test_command: str,
                           image: str, runtime: str, ci_defaults: dict,
                           pr_label: str) -> dict:
    """Run a test command on a branch. Returns result dict."""
    timeout = ci_defaults.get("timeout", 600)
    memory = ci_defaults.get("memory", "2g")
    cpus = ci_defaults.get("cpus", "2")

    script = f"""
set -e
cd /repo

git checkout --quiet '{pr_branch}' 2>&1
COMMIT=$(git rev-parse --short HEAD)

echo '=== Running test command ==='
set +e
{test_command} 2>&1
TEST_RC=$?
set -e
echo "=== Test exit code: $TEST_RC ==="

echo '---RESULTS---'
echo "COMMIT=$COMMIT"
echo "TEST_RC=$TEST_RC"
"""

    try:
        r = subprocess.run(
            _build_test_docker_args(repo_dir, image, runtime, memory, cpus,
                                    pr_label, script),
            capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"success": False, "output": "Verification timed out.",
                "commit": "", "test_rc": -1}

    output = r.stdout + r.stderr
    result: dict = {"output": output, "commit": "", "test_rc": -1}

    if "---RESULTS---" in output:
        for line in output.split("---RESULTS---")[1].splitlines():
            line = line.strip()
            if line.startswith("COMMIT="):
                result["commit"] = line.split("=", 1)[1]
            elif line.startswith("TEST_RC="):
                result["test_rc"] = int(line.split("=", 1)[1])

    result["success"] = (result["test_rc"] == 0)
    return result


def _run_fixed_command_verification(repo_dir: str, branch: str, command: str,
                                    image: str, runtime: str, cmd_cfg: dict,
                                    ci_defaults: dict, pr_label: str) -> dict:
    """Run a fixed command from ci-commands.json on a branch."""
    timeout = cmd_cfg.get("timeout", ci_defaults.get("timeout", 600))
    memory = cmd_cfg.get("memory", ci_defaults.get("memory", "2g"))
    cpus = cmd_cfg.get("cpus", ci_defaults.get("cpus", "2"))

    script = f"""
set -e
cd /repo

git checkout --quiet '{branch}' 2>&1
COMMIT=$(git rev-parse --short HEAD)

echo '=== Running: {command} ==='
set +e
{command} 2>&1
TEST_RC=$?
set -e
echo "=== Exit code: $TEST_RC ==="

echo '---RESULTS---'
echo "COMMIT=$COMMIT"
echo "TEST_RC=$TEST_RC"
"""

    try:
        r = subprocess.run(
            _build_test_docker_args(repo_dir, image, runtime, memory, cpus,
                                    pr_label, script),
            capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"success": False, "output": "Verification timed out.",
                "commit": "", "test_rc": -1}

    output = r.stdout + r.stderr
    result: dict = {"output": output, "commit": "", "test_rc": -1}

    if "---RESULTS---" in output:
        for line in output.split("---RESULTS---")[1].splitlines():
            line = line.strip()
            if line.startswith("COMMIT="):
                result["commit"] = line.split("=", 1)[1]
            elif line.startswith("TEST_RC="):
                result["test_rc"] = int(line.split("=", 1)[1])

    result["success"] = (result["test_rc"] == 0)
    return result


# ─── Result formatting ────────────────────────────────────────────────────────


def _format_bug_result(result: dict, test_file: str,
                       base_branch: str, pr_branch: str) -> str:
    """Format a bug verification result as markdown."""
    status = "PASS" if result["success"] else "FAIL"
    base_ok = "pass" if result["base_rc"] != 0 else "FAIL"
    pr_ok = "pass" if result["pr_rc"] == 0 else "FAIL"

    body = f"""## CI: {test_file}

**Tested commit:** `{result.get('commit', 'unknown')}`
**Status:** {status}

| Step | Result |
|------|--------|
| Repro fails on base (`{base_branch}`) | {base_ok} (exit {result['base_rc']}) |
| Repro passes on PR (`{pr_branch}`) | {pr_ok} (exit {result['pr_rc']}) |"""

    output = result.get("output", "")
    lines = output.splitlines()
    if len(lines) > 50:
        output = "\n".join(["...(truncated)"] + lines[-50:])
    body += f"\n\n<details><summary>Last 50 lines</summary>\n\n```\n{output}\n```\n\n</details>"
    return body


def _format_test_result(result: dict, test_command: str, pr_branch: str) -> str:
    """Format a test command result as markdown."""
    status = "PASS" if result["success"] else "FAIL"

    body = f"""## CI: `{test_command}`

**Tested commit:** `{result.get('commit', 'unknown')}`
**Branch:** `{pr_branch}`
**Status:** {status}"""

    output = result.get("output", "")
    lines = output.splitlines()
    if len(lines) > 50:
        output = "\n".join(["...(truncated)"] + lines[-50:])
    body += f"\n\n<details><summary>Last 50 lines</summary>\n\n```\n{output}\n```\n\n</details>"
    return body


# ─── Gitea helpers ────────────────────────────────────────────────────────────


def _ci_token(cfg: Config) -> str:
    """Return the CI watch Gitea token, falling back to admin token."""
    return cfg.ci_watch_gitea_token or cfg.gitea_admin_token


def _gitea_post_comment(cfg: Config, repo_path: str,
                        issue_number: int, body: str) -> dict:
    """Post a comment on a Gitea PR as sandbox-ci. Returns the comment object."""
    url = f"http://localhost:{cfg.gitea_port}/api/v1/repos/{repo_path}/issues/{issue_number}/comments"
    data = json.dumps({"body": body}).encode()
    req = Request(url, data=data, method="POST", headers={
        "Authorization": f"token {_ci_token(cfg)}",
        "Content-Type": "application/json",
    })
    with urlopen(req, timeout=30) as resp:
        content = resp.read().decode()
        return json.loads(content) if content.strip() else {}


def _gitea_attach_file(cfg: Config, repo_path: str,
                       comment_id: int, filepath: str, filename: str) -> None:
    """Attach a file to a Gitea comment as sandbox-ci."""
    url = f"http://localhost:{cfg.gitea_port}/api/v1/repos/{repo_path}/issues/comments/{comment_id}/assets"
    boundary = "----CIWatchBoundary"
    file_content = Path(filepath).read_bytes()

    body_parts = [
        f"--{boundary}\r\n".encode(),
        f'Content-Disposition: form-data; name="attachment"; filename="{filename}"\r\n'.encode(),
        b"Content-Type: application/octet-stream\r\n\r\n",
        file_content,
        f"\r\n--{boundary}--\r\n".encode(),
    ]
    body_bytes = b"".join(body_parts)

    req = Request(url, data=body_bytes, method="POST", headers={
        "Authorization": f"token {_ci_token(cfg)}",
        "Content-Type": f"multipart/form-data; boundary={boundary}",
    })
    urlopen(req, timeout=30)


# ─── Polling loop ─────────────────────────────────────────────────────────────


def _ci_log(msg: str) -> None:
    """Print a timestamped log message."""
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def _ci_watch_loop(cfg: Config) -> None:
    """Main CI watch polling loop. Runs until killed."""
    ci_config = _load_ci_commands()
    patterns = _build_ci_command_patterns(ci_config)
    defaults = ci_config.get("defaults", {})
    rate_limit = defaults.get("rate_limit", 10)
    rate_window = defaults.get("rate_window", 3600)

    CI_LOGS_DIR.mkdir(parents=True, exist_ok=True)

    seen_comment_ids: set[int] = set()
    poll_interval = cfg.ci_watch_poll_interval

    _ci_log(f"CI watch started (poll every {poll_interval}s)")
    _ci_log(f"Commands: {', '.join(ci_config.get('commands', {}).keys())}")

    while True:
        try:
            _ci_watch_poll(cfg, patterns, defaults, rate_limit, rate_window, seen_comment_ids)
        except KeyboardInterrupt:
            _ci_log("CI watch stopped.")
            break
        except Exception as e:
            _ci_log(f"Poll error: {e}")

        # Prune seen set (keep last 10000 IDs to bound memory)
        if len(seen_comment_ids) > 10000:
            sorted_ids = sorted(seen_comment_ids)
            seen_comment_ids.clear()
            seen_comment_ids.update(sorted_ids[-5000:])

        time.sleep(poll_interval)


def _ci_watch_poll(cfg: Config, patterns: list, defaults: dict,
                   rate_limit: int, rate_window: int,
                   seen_comment_ids: set[int]) -> None:
    """Single poll iteration: check all agent repos for CI commands."""
    # Get all agent users
    try:
        users = gitea_api(cfg, "GET", "/admin/users?limit=50")
    except RuntimeError:
        return
    if not isinstance(users, list):
        return

    agent_users = [u for u in users
                   if isinstance(u, dict) and u.get("login", "").startswith("agent-")]

    for user in agent_users:
        login = user["login"]
        # Get user's repos
        try:
            repos = gitea_api(cfg, "GET", f"/repos/search?owner={login}&limit=50")
        except RuntimeError:
            continue
        if isinstance(repos, dict):
            repos = repos.get("data", [])
        if not isinstance(repos, list):
            continue

        for repo in repos:
            if not isinstance(repo, dict):
                continue
            repo_full = repo.get("full_name", "")
            if not repo_full:
                continue

            # Get open PRs
            try:
                prs = gitea_api(cfg, "GET", f"/repos/{repo_full}/pulls?state=open&limit=50")
            except RuntimeError:
                continue
            if not isinstance(prs, list):
                continue

            for pr in prs:
                if not isinstance(pr, dict):
                    continue
                pr_number = pr.get("number", 0)
                if not pr_number:
                    continue

                # Get comments on this PR
                try:
                    comments = gitea_api(
                        cfg, "GET",
                        f"/repos/{repo_full}/issues/{pr_number}/comments?limit=50")
                except RuntimeError:
                    continue
                if not isinstance(comments, list):
                    continue

                for comment in comments:
                    if not isinstance(comment, dict):
                        continue
                    comment_id = comment.get("id", 0)
                    if not comment_id or comment_id in seen_comment_ids:
                        continue

                    seen_comment_ids.add(comment_id)

                    comment_body = comment.get("body", "")
                    parsed = _parse_ci_command(comment_body, patterns)
                    if not parsed:
                        continue

                    cmd_name, cmd_args, cmd_cfg = parsed
                    project = login.removeprefix("agent-")
                    pr_key = f"{repo_full}#{pr_number}"
                    pr_label = pr_key

                    _ci_log(f"{cmd_name} on {pr_key} (branch: {cmd_args.get('branch', '')})")

                    # Rate limit
                    if not _check_rate_limit(pr_key, rate_limit, rate_window):
                        _gitea_post_comment(
                            cfg, repo_full, pr_number,
                            f"**Rate limited.** Max {rate_limit} CI runs per PR per hour.")
                        continue

                    # Validate
                    error = _validate_ci_args(cmd_name, cmd_args, cmd_cfg, cfg, repo_full)
                    if error:
                        _gitea_post_comment(
                            cfg, repo_full, pr_number,
                            f"**CI error:** {error}")
                        continue

                    # Execute
                    _ci_watch_execute(
                        cfg, repo_full, pr_number, pr, cmd_name,
                        cmd_args, cmd_cfg, defaults, project, pr_label)


def _ci_watch_execute(cfg: Config, repo_full: str, pr_number: int,
                      pr: dict, cmd_name: str, cmd_args: dict,
                      cmd_cfg: dict, defaults: dict,
                      project: str, pr_label: str) -> None:
    """Clone repo, run test in container, post results."""
    branch = cmd_args["branch"]
    base_branch = pr.get("base", {}).get("ref", "main")
    head_sha = pr.get("head", {}).get("sha", "")
    agent_info = _get_agent_container_info(project)
    image = agent_info["image"]
    runtime = agent_info["runtime"]
    job_id = str(uuid.uuid4())[:8]

    # Clone repo to temp dir
    clone_url = f"http://x-access-token:{cfg.gitea_admin_token}@localhost:{cfg.gitea_port}/{repo_full}.git"
    repo_dir = tempfile.mkdtemp(prefix=f"ci-{job_id}-")

    try:
        _ci_log(f"  Cloning {repo_full} to {repo_dir}")
        r = subprocess.run(
            ["git", "clone", "--quiet", clone_url, repo_dir],
            capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            _gitea_post_comment(
                cfg, repo_full, pr_number,
                f"**CI error:** Failed to clone repository.\n```\n{r.stderr}\n```")
            return

        # Run test
        cmd_type = cmd_cfg.get("type", "")
        if cmd_type == "bug-verification":
            result = _run_bug_verification(
                repo_dir, branch, base_branch,
                cmd_args["test_file"], image, runtime, defaults, pr_label)
            body = _format_bug_result(result, cmd_args["test_file"], base_branch, branch)
        elif cmd_type == "command":
            result = _run_test_verification(
                repo_dir, branch, cmd_args["test_command"],
                image, runtime, defaults, pr_label)
            body = _format_test_result(result, cmd_args["test_command"], branch)
        elif cmd_type == "fixed-command":
            fixed_cmd = cmd_cfg.get("command", "")
            result = _run_fixed_command_verification(
                repo_dir, branch, fixed_cmd, image, runtime, cmd_cfg, defaults, pr_label)
            body = _format_test_result(result, fixed_cmd, branch)
        else:
            _ci_log(f"  Unknown command type: {cmd_type}")
            return

        # Check for stale HEAD
        if head_sha and result.get("commit") and \
                not head_sha.startswith(result["commit"]):
            body += (f"\n\n**Note:** PR HEAD (`{head_sha[:7]}`) differs from "
                     f"tested commit (`{result['commit']}`). Result may be stale.")

        # Save full log
        log_file = CI_LOGS_DIR / f"{job_id}.log"
        log_file.write_text(result.get("output", ""))
        _ci_log(f"  Log saved: {log_file}")

        # Post result
        comment = _gitea_post_comment(cfg, repo_full, pr_number, body)
        comment_id = comment.get("id", 0) if isinstance(comment, dict) else 0

        # Attach full log
        if comment_id and log_file.exists():
            try:
                _gitea_attach_file(cfg, repo_full, comment_id,
                                   str(log_file), f"ci-{job_id}.log")
            except Exception as e:
                _ci_log(f"  Warning: could not attach log: {e}")

        status = "PASS" if result.get("success") else "FAIL"
        _ci_log(f"  Result: {status}")

    finally:
        # Cleanup temp dir
        shutil.rmtree(repo_dir, ignore_errors=True)


# ─── CLI commands ─────────────────────────────────────────────────────────────


def ci_watch_pid() -> int | None:
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


CI_USER = "sandbox-ci"


def _ensure_ci_user(cfg: Config) -> str:
    """Create the sandbox-ci Gitea user and return an API token for it.

    Idempotent: if the user already exists, resets its password and
    regenerates the token.
    """
    user_pass = gen_password()

    if not gitea_api_ok(cfg, "GET", f"/users/{CI_USER}"):
        print(f"Creating Gitea user: {CI_USER}...")
        gitea_api(cfg, "POST", "/admin/users", {
            "username": CI_USER,
            "password": user_pass,
            "email": f"{CI_USER}@sandbox.local",
            "must_change_password": False,
            "visibility": "public",
        })
    else:
        # User exists — reset password so we can generate a new token
        gitea_api(cfg, "PATCH", f"/admin/users/{CI_USER}", {
            "login_name": CI_USER,
            "source_id": 0,
            "password": user_pass,
            "must_change_password": False,
        })

    # Generate token with scopes needed for posting CI results
    base = f"http://localhost:{cfg.gitea_port}/api/v1/users/{CI_USER}/tokens"
    try:
        existing = http_basic_auth_request(base, CI_USER, user_pass)
        if isinstance(existing, list):
            for tok in existing:
                if "id" in tok:
                    http_basic_auth_request(
                        f"{base}/{tok['id']}", CI_USER, user_pass, method="DELETE")
    except Exception:
        pass

    resp = http_basic_auth_request(base, CI_USER, user_pass, method="POST", body={
        "name": "ci-watch-token",
        "scopes": [
            "write:issue",        # post comments, attach files
            "read:repository",    # read PRs, branches, file contents
            "read:user",          # auth
        ],
    })
    token = resp.get("sha1") or resp.get("token") or ""
    if not token:
        die(f"Failed to generate Gitea token for {CI_USER}: {resp}")

    # Add sandbox-ci as collaborator on all existing agent repos
    _grant_ci_access_all(cfg)

    return token


def _grant_ci_access_all(cfg: Config) -> None:
    """Add sandbox-ci as read collaborator on all agent repos."""
    try:
        users = gitea_api(cfg, "GET", "/admin/users?limit=50")
    except RuntimeError:
        return
    if not isinstance(users, list):
        return

    for user in users:
        if not isinstance(user, dict):
            continue
        login = user.get("login", "")
        if not login.startswith("agent-"):
            continue
        try:
            repos = gitea_api(cfg, "GET", f"/repos/search?owner={login}&limit=50")
        except RuntimeError:
            continue
        if isinstance(repos, dict):
            repos = repos.get("data", [])
        if not isinstance(repos, list):
            continue
        for repo in repos:
            if not isinstance(repo, dict):
                continue
            repo_full = repo.get("full_name", "")
            if repo_full:
                gitea_api_ok(cfg, "PUT",
                             f"/repos/{repo_full}/collaborators/{CI_USER}",
                             {"permission": "read"})


def cmd_setup() -> None:
    """Interactive CI watch configuration."""
    cfg = load_config()
    print("=== CI Watch Setup ===\n")

    interval = input("Poll interval in seconds [5]: ").strip() or "5"
    try:
        int(interval)
    except ValueError:
        die(f"Invalid interval: {interval}")

    # Create sandbox-ci Gitea user and token
    token = _ensure_ci_user(cfg)

    update_env_key("CI_WATCH_ENABLED", "true")
    update_env_key("CI_WATCH_POLL_INTERVAL", interval)
    update_env_key("CI_WATCH_GITEA_TOKEN", token)

    print(f"\nCI watch enabled (poll every {interval}s).")
    print(f"CI results will be posted by '{CI_USER}'.")
    print("Commands are defined in ci-commands.json.")
    print("Run 'sandbox ci-watch start' or 'sandbox up' to start.")


def cmd_start() -> None:
    """Start the CI watch background process."""
    pid = ci_watch_pid()
    if pid:
        print(f"CI watch is already running (PID {pid}).")
        return

    if not CI_COMMANDS_FILE.exists():
        die("ci-commands.json not found. Cannot start CI watch.")

    # Start background process
    log_fd = open(CI_WATCH_LOG_FILE, "a")
    proc = subprocess.Popen(
        [sys.executable, str(SCRIPT_DIR / "ci-watch.py"), "_loop"],
        stdout=log_fd,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        cwd=str(SCRIPT_DIR),
    )
    log_fd.close()

    CI_WATCH_PID_FILE.write_text(str(proc.pid))
    print(f"CI watch started (PID {proc.pid}).")
    print(f"Log: {CI_WATCH_LOG_FILE}")


def cmd_stop() -> None:
    """Stop the CI watch background process."""
    pid = ci_watch_pid()
    if not pid:
        print("CI watch is not running.")
        return

    try:
        os.kill(pid, signal.SIGTERM)
        # Wait briefly for clean shutdown
        for _ in range(10):
            try:
                os.kill(pid, 0)
                time.sleep(0.2)
            except ProcessLookupError:
                break
    except ProcessLookupError:
        pass

    CI_WATCH_PID_FILE.unlink(missing_ok=True)
    print(f"CI watch stopped (was PID {pid}).")


def cmd_loop() -> None:
    """Internal: run the CI watch polling loop (called as subprocess)."""
    cfg = load_config()
    _ci_watch_loop(cfg)


# ─── Main ─────────────────────────────────────────────────────────────────────


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        prog="ci-watch",
        description="CI watch background process for the LLM Agent Sandbox")
    sub = parser.add_subparsers(dest="action", required=True)

    sub.add_parser("setup", help="Configure CI watch")
    sub.add_parser("start", help="Start CI watch background process")
    sub.add_parser("stop", help="Stop CI watch background process")
    sub.add_parser("_loop", help=argparse.SUPPRESS)  # Internal

    args = parser.parse_args()

    actions = {
        "setup": cmd_setup,
        "start": cmd_start,
        "stop": cmd_stop,
        "_loop": cmd_loop,
    }

    try:
        actions[args.action]()
    except KeyboardInterrupt:
        sys.exit(130)
    except RuntimeError as e:
        die(str(e))


if __name__ == "__main__":
    main()
