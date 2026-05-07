#!/usr/bin/env python3
"""fetch-sandbox — Fetch agent branch from sandbox Gitea, run security review and safety checks.

Usage:
    python fetch-sandbox.py <project> [<repo_path>] (--pr <N> | --branch <name> | --commit <sha>) [--skip-review]
    python fetch-sandbox.py setup

    project        Sandbox project name (e.g. myrepo)
    repo_path      Path to your local git repository (default: cwd)
    --pr           Fetch PR #N from Gitea (head branch)
    --branch       Fetch a branch by name
    --commit       Fetch a specific commit SHA (Gitea must allow uploadpack.allowAnySHA1InWant)
    --skip-review  Skip the LLM security review step
    setup          Configure LLM provider for security reviews (interactive)

Diff for review/safety is always HEAD...<ref> — what `git merge --squash` will apply.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

SCRIPT_DIR = Path(__file__).resolve().parent

AUTO_EXEC_PATHS = [
    ".envrc", ".vscode/", ".husky/", ".pre-commit-config.yaml", ".gitmodules",
    "package.json", "setup.py", "setup.cfg", "Makefile", "CMakeLists.txt",
    ".cargo/config.toml", ".eslintrc.js", ".prettierrc.js",
]

NO_ISSUES_TEXT = "**No security concerns found.**"

# Default API endpoints per provider (override with REVIEWER_ENDPOINT in .env).
PROVIDER_ENDPOINTS = {
    "anthropic": "https://api.anthropic.com",
    "openai": "https://api.openai.com",
    "openrouter": "https://openrouter.ai/api",
}

DEFAULT_PROMPT = "Review this diff for security issues:\n\n{diff}\n"


# ─── Config ───────────────────────────────────────────────────────────────────


def load_env() -> tuple[str, str]:
    """Load GITEA_PORT and GITEA_ADMIN_TOKEN from .env. Returns (port, token)."""
    env_file = SCRIPT_DIR / ".env"
    if not env_file.exists():
        die(".env not found. Copy .env.example to .env and fill in values.")

    gitea_port = "3000"
    gitea_admin_token = ""

    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip()
            if key == "GITEA_PORT":
                gitea_port = value
            elif key == "GITEA_ADMIN_TOKEN":
                gitea_admin_token = value

    if not gitea_admin_token:
        die("GITEA_ADMIN_TOKEN not set in .env. Run 'sandbox setup' first.")

    return gitea_port, gitea_admin_token


def load_review_config() -> tuple[str, str, str, str] | None:
    """Load REVIEWER_* settings from .env. Returns (provider, key, model, endpoint) or None."""
    env_file = SCRIPT_DIR / ".env"
    if not env_file.exists():
        return None

    provider = ""
    api_key = ""
    model = ""
    endpoint = ""

    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip()
            if key == "REVIEWER_PROVIDER":
                provider = value
            elif key == "REVIEWER_API_KEY":
                api_key = value
            elif key == "REVIEWER_MODEL":
                model = value
            elif key == "REVIEWER_ENDPOINT":
                endpoint = value

    if not model:
        return None
    return provider or "anthropic", api_key, model, endpoint


def load_max_diff_size() -> int:
    """Load REVIEWER_MAX_DIFF_SIZE from .env. 0 = no truncation. Default: 0."""
    env_file = SCRIPT_DIR / ".env"
    if not env_file.exists():
        return 0
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip() == "REVIEWER_MAX_DIFF_SIZE":
            try:
                return max(0, int(value.strip()))
            except ValueError:
                return 0
    return 0


def load_yaml_config(path: Path) -> dict:
    """Minimal YAML loader — handles top-level scalars and block scalars (|)."""
    config: dict = {}
    if not path.exists():
        return config

    lines = path.read_text().splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        if not stripped or stripped.startswith("#"):
            i += 1
            continue

        if line[0] != " " and ":" in stripped:
            key, _, value = stripped.partition(":")
            key, value = key.strip(), value.strip()

            if value == "|":
                block_lines: list[str] = []
                i += 1
                while i < len(lines):
                    bline = lines[i]
                    if bline and not bline[0].isspace():
                        break
                    block_lines.append(bline)
                    i += 1
                non_empty = [bl for bl in block_lines if bl.strip()]
                if non_empty:
                    indent = min(len(bl) - len(bl.lstrip()) for bl in non_empty)
                    block_lines = [bl[indent:] if len(bl) > indent else "" for bl in block_lines]
                config[key] = "\n".join(block_lines).rstrip("\n") + "\n"
                continue

            config[key] = value
        i += 1

    return config


# ─── Helpers ──────────────────────────────────────────────────────────────────


def die(msg: str) -> None:
    print(f"Error: {msg}", file=sys.stderr)
    sys.exit(1)


def gitea_get(api_base: str, token: str, path: str) -> dict | list | str:
    """GET from Gitea API, return parsed JSON."""
    req = Request(f"{api_base}{path}", headers={
        "Authorization": f"token {token}",
        "Content-Type": "application/json",
    })
    with urlopen(req, timeout=30) as resp:
        content = resp.read().decode()
        ct = resp.headers.get("Content-Type", "")
        if "application/json" in ct and content:
            return json.loads(content)
        return content


def git(repo_path: str, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    """Run git command in repo_path."""
    return subprocess.run(
        ["git", "-C", repo_path, *args],
        capture_output=True, text=True, check=check,
    )


# ─── LLM security review ────────────────────────────────────────────────────


def call_anthropic(prompt: str, api_key: str, model: str, endpoint: str, max_tokens: int) -> str:
    """Call the Anthropic Messages API."""
    body = json.dumps({
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()

    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }

    req = Request(f"{endpoint}/v1/messages", data=body, headers=headers, method="POST")

    try:
        with urlopen(req, timeout=120) as resp:
            result = json.loads(resp.read().decode())
            for block in result.get("content", []):
                if block.get("type") == "text":
                    return block["text"]
            return "*Review returned empty response.*"
    except (HTTPError, URLError) as e:
        return f"*Review failed: {e}*"


def call_openai_compatible(prompt: str, api_key: str, model: str, endpoint: str,
                           max_tokens: int) -> str:
    """Call an OpenAI-compatible Chat Completions API (OpenAI, OpenRouter, local)."""
    body = json.dumps({
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()

    headers: dict[str, str] = {"content-type": "application/json"}
    if api_key:
        headers["authorization"] = f"Bearer {api_key}"

    req = Request(f"{endpoint}/v1/chat/completions", data=body, headers=headers, method="POST")

    try:
        with urlopen(req, timeout=120) as resp:
            result = json.loads(resp.read().decode())
            choices = result.get("choices", [])
            if choices:
                return choices[0].get("message", {}).get("content", "*Empty response.*")
            return "*Review returned empty response.*"
    except (HTTPError, URLError) as e:
        return f"*Review failed: {e}*"


PROVIDERS = {
    "anthropic": call_anthropic,
    "openai": call_openai_compatible,
    "openrouter": call_openai_compatible,
    "local": call_openai_compatible,
}


def call_llm(diff: str, provider: str, api_key: str, model: str, endpoint: str,
             prompt_template: str, max_diff_size: int, max_tokens: int) -> str:
    """Send the diff to the configured LLM for security review."""
    if not api_key and provider != "local":
        return "*Review skipped: REVIEWER_API_KEY not configured.*"

    provider_fn = PROVIDERS.get(provider)
    if not provider_fn:
        return f"*Review skipped: unknown provider '{provider}'.*"

    truncated = diff if max_diff_size <= 0 else diff[:max_diff_size]
    prompt = prompt_template.format(diff=truncated)
    return provider_fn(prompt, api_key, model, endpoint, max_tokens)


def review_diff(repo_path: str, ref: str) -> bool:
    """Compute git diff against HEAD, send to LLM, display review. Returns True if safe to proceed."""
    print("\n── Security Review (LLM) ──")

    review_cfg = load_review_config()
    if not review_cfg:
        print("  (Reviewer not configured. Run 'fetch-sandbox.py setup' to enable.)")
        return True

    provider, api_key, model, endpoint = review_cfg

    if not endpoint:
        endpoint = PROVIDER_ENDPOINTS.get(provider, "")
    if not endpoint:
        print(f"  (No endpoint for provider '{provider}'. Set REVIEWER_ENDPOINT in .env.)")
        return True
    endpoint = endpoint.rstrip("/")

    # Load prompt + token limit from yaml; diff-size limit comes from .env (set by `setup`)
    yaml_cfg = load_yaml_config(SCRIPT_DIR / "review-config.yaml")
    prompt_template = yaml_cfg.get("prompt", DEFAULT_PROMPT)
    max_tokens = int(yaml_cfg.get("max_tokens", 4096))
    max_diff_size = load_max_diff_size()

    # Compute diff against current checkout (matches what `merge --squash` will apply)
    r = git(repo_path, "diff", f"HEAD...{ref}", check=False)
    diff = r.stdout
    if not diff:
        print("  (No diff to review — branch matches HEAD.)")
        return True

    print(f"  Provider: {provider} | Model: {model}")
    if max_diff_size <= 0:
        print(f"  Diff size: {len(diff)} chars (no truncation)")
    else:
        print(f"  Diff size: {len(diff)} chars (limit: {max_diff_size})")
        if len(diff) > max_diff_size:
            print(f"  Warning: diff truncated from {len(diff)} to {max_diff_size} chars")
    print("  Sending to LLM...", end=" ", flush=True)

    review = call_llm(diff, provider, api_key, model, endpoint,
                      prompt_template, max_diff_size, max_tokens)
    print("done.\n")

    if NO_ISSUES_TEXT in review and "### [SEVERITY:" not in review:
        print(f"  {NO_ISSUES_TEXT}")
        return True

    print(review)
    print()
    answer = input("Security issues found. Proceed with merge? [y/N] ").strip()
    return answer.lower() in ("y", "yes")


# ─── Safety checks ────────────────────────────────────────────────────────────


def run_safety_checks(repo_path: str, ref: str) -> None:
    """Check for symlinks and auto-execute file modifications (diff against HEAD)."""
    print("\n── Pre-Merge Safety Checks ──")

    # Symlinks
    r = git(repo_path, "ls-tree", "-r", "--full-tree", ref, check=False)
    symlinks = [line for line in r.stdout.splitlines() if line.startswith("120000")]
    if symlinks:
        print("\n  \u26a0  SYMLINKS DETECTED:")
        for line in symlinks:
            parts = line.split(None, 3)
            if len(parts) == 4:
                _, _, blob_hash, path = parts
                target_r = git(repo_path, "cat-file", "-p", blob_hash, check=False)
                print(f"    {path} -> {target_r.stdout.strip()}")
        print()
    else:
        print("  Symlinks: none")

    r = git(repo_path, "diff", "--quiet", f"HEAD...{ref}", "--", *AUTO_EXEC_PATHS, check=False)
    if r.returncode != 0:
        print("  \u26a0  Auto-execute files: MODIFIED")
    else:
        print("  Auto-execute files: unchanged")


# ─── Review setup ─────────────────────────────────────────────────────────────


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


def cmd_setup() -> None:
    """Interactive reviewer configuration — configure LLM provider, key, model."""
    print("=== Reviewer Setup ===\n")

    # Provider
    providers = ["anthropic", "openai", "openrouter", "local"]
    print(f"LLM provider ({', '.join(providers)}): ", end="", flush=True)
    provider = input().strip()
    if provider not in providers:
        die(f"Invalid provider '{provider}'. Must be one of: {', '.join(providers)}")

    # API key (skip for local)
    api_key = ""
    if provider != "local":
        current_key = ""
        review_cfg = load_review_config()
        if review_cfg:
            current_key = review_cfg[1]
        masked = f"{current_key[:8]}...{current_key[-4:]}" if len(current_key) > 12 else current_key
        print(f"API key [{masked or 'not set'}]: ", end="", flush=True)
        choice = input().strip()
        api_key = choice if choice else current_key
        if not api_key:
            die("API key is required for non-local providers.")

    # Model
    print("Model: ", end="", flush=True)
    model = input().strip()
    if not model:
        die("Model name is required.")

    # Endpoint (required for local, optional for others)
    endpoint = ""
    if provider == "local":
        current_ep = ""
        review_cfg = load_review_config()
        if review_cfg:
            current_ep = review_cfg[3]
        print(f"Endpoint [{current_ep or 'not set'}]: ", end="", flush=True)
        choice = input().strip()
        endpoint = choice if choice else current_ep
        if not endpoint:
            die("Endpoint is required for local provider.")

    # Health check — verify credentials before saving
    print("\nVerifying credentials...", end=" ", flush=True)
    check_ep = (endpoint or PROVIDER_ENDPOINTS.get(provider, "")).rstrip("/")
    if provider == "anthropic":
        check_url = f"{check_ep}/v1/messages"
        check_headers: dict[str, str] = {
            "x-api-key": api_key, "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
    else:
        check_url = f"{check_ep}/v1/chat/completions"
        check_headers = {"content-type": "application/json"}
        if api_key:
            check_headers["authorization"] = f"Bearer {api_key}"
    check_body = json.dumps({
        "model": model, "max_tokens": 1,
        "messages": [{"role": "user", "content": "Say OK"}],
    }).encode()
    try:
        req = Request(check_url, data=check_body, method="POST", headers=check_headers)
        with urlopen(req, timeout=30) as resp:
            resp.read()
        print("OK")
    except HTTPError as e:
        body = e.read().decode(errors="replace")[:300]
        die(f"health check failed — POST {check_url} returned HTTP {e.code}: {body}")
    except URLError as e:
        die(f"health check failed — POST {check_url}: {e.reason}")

    # Max diff size (chars sent to the LLM; 0 = no truncation)
    current_max = load_max_diff_size()
    print(f"Max diff size in chars (0 = no truncation) [{current_max}]: ", end="", flush=True)
    choice = input().strip()
    if not choice:
        max_diff_size = current_max
    else:
        try:
            max_diff_size = max(0, int(choice))
        except ValueError:
            die(f"Max diff size must be a non-negative integer, got {choice!r}")

    # Write to .env
    print("Saving configuration...")
    update_env_key("REVIEWER_PROVIDER", provider)
    update_env_key("REVIEWER_API_KEY", api_key)
    update_env_key("REVIEWER_MODEL", model)
    if endpoint:
        update_env_key("REVIEWER_ENDPOINT", endpoint)
    update_env_key("REVIEWER_MAX_DIFF_SIZE", str(max_diff_size))

    diff_label = "no truncation" if max_diff_size <= 0 else f"{max_diff_size} chars"
    print(f"""
=== Reviewer Ready ===
Provider:  {provider}
Model:     {model}
Max diff:  {diff_label}
Reviews will run automatically when you use fetch-sandbox.py.""")


# ─── Main ─────────────────────────────────────────────────────────────────────


def _run_review_and_merge(repo_path: str, ref: str, skip_review: bool) -> None:
    """Run safety checks, optional LLM review, and squash-merge."""
    # Safety checks
    run_safety_checks(repo_path, ref)

    # LLM security review
    if not skip_review:
        safe = review_diff(repo_path, ref)
        if not safe:
            print("\nMerge cancelled by user after security review.")
            return

    # Merge
    print(f"\n{'=' * 64}")

    r = git(repo_path, "rev-parse", "--abbrev-ref", "HEAD", check=False)
    local_branch = r.stdout.strip() if r.returncode == 0 else "unknown"

    r = git(repo_path, "merge", "--squash", ref, check=False)
    if r.returncode != 0:
        print(f"\nMerge failed:\n{r.stderr.strip()}")
        print("Resolve conflicts or stash local changes and retry.")
        return

    git(repo_path, "reset", "HEAD", check=False)
    print(f"\nDone — changes applied as unstaged modifications on {local_branch}.")


def _print_usage() -> None:
    print("Usage: python fetch-sandbox.py <project> [<repo_path>] (--pr <N> | --branch <name> | --commit <sha>) [--skip-review]")
    print("       python fetch-sandbox.py setup")
    print()
    print("  project        Sandbox project name")
    print("  repo_path      Path to your local git repository (default: cwd)")
    print("  --pr <N>       Fetch PR #N from Gitea (head branch)")
    print("  --branch <X>   Fetch branch X")
    print("  --commit <S>   Fetch commit SHA S directly")
    print("  --skip-review  Skip the LLM security review step")
    print("  setup          Configure LLM provider for security reviews")


def _resolve_pr(api_base: str, token: str, project: str, pr_id: int) -> tuple[str, str]:
    """Look up a PR by ID. Returns (head_ref, base_ref)."""
    gitea_user = f"agent-{project}"
    try:
        data = gitea_get(api_base, token, f"/api/v1/repos/{gitea_user}/{project}/pulls/{pr_id}")
    except HTTPError as e:
        if e.code == 404:
            die(f"PR #{pr_id} not found in {gitea_user}/{project}")
        die(f"Gitea API error fetching PR #{pr_id}: {e}")
    except URLError as e:
        die(f"Cannot reach Gitea at {api_base}: {e}")
    if not isinstance(data, dict):
        die(f"Unexpected response shape for PR #{pr_id}")
    head = (data.get("head") or {}).get("ref")
    base = (data.get("base") or {}).get("ref")
    if not head or not base:
        die(f"PR #{pr_id} response missing head/base refs")
    state = data.get("state", "?")
    merged = data.get("merged", False)
    title = data.get("title", "")
    status = "merged" if merged else state
    print(f"  PR #{pr_id} [{status}]: {head} → {base}")
    if title:
        print(f"  Title: {title}")
    return head, base


def main() -> None:
    # Dispatch subcommand
    if len(sys.argv) >= 2 and sys.argv[1] == "setup":
        cmd_setup()
        return

    skip_review = False
    pr_id: int | None = None
    branch = ""
    commit = ""
    positional: list[str] = []
    i = 1
    while i < len(sys.argv):
        a = sys.argv[i]
        if a == "--skip-review":
            skip_review = True
            i += 1
            continue
        if a == "--pr" and i + 1 < len(sys.argv):
            try:
                pr_id = int(sys.argv[i + 1])
            except ValueError:
                die(f"--pr expects an integer, got {sys.argv[i + 1]!r}")
            i += 2
            continue
        if a == "--branch" and i + 1 < len(sys.argv):
            branch = sys.argv[i + 1]
            i += 2
            continue
        if a == "--commit" and i + 1 < len(sys.argv):
            commit = sys.argv[i + 1].strip()
            if not all(c in "0123456789abcdefABCDEF" for c in commit) or not (4 <= len(commit) <= 64):
                die(f"--commit expects a hex SHA (4-64 chars), got {commit!r}")
            i += 2
            continue
        if a in ("-h", "--help"):
            _print_usage()
            return
        positional.append(a)
        i += 1

    if not positional:
        _print_usage()
        sys.exit(1)
    selectors = sum(x is not None and x != "" for x in (pr_id, branch, commit))
    if selectors != 1:
        die("Specify exactly one of --pr <N>, --branch <name>, or --commit <sha>")

    project = positional[0]
    repo_path = os.path.abspath(positional[1]) if len(positional) > 1 else os.getcwd()

    # Validate git repo
    r = subprocess.run(["git", "-C", repo_path, "rev-parse", "--is-inside-work-tree"],
                       capture_output=True, text=True, check=False)
    if r.returncode != 0:
        die(f"Not a git repository: {repo_path}")

    # Load config
    gitea_port, gitea_admin_token = load_env()
    gitea_user = f"agent-{project}"
    api_base = f"http://localhost:{gitea_port}"
    sandbox_url = f"{api_base}/{gitea_user}/{project}.git"

    print(f"{'=' * 64}")
    if pr_id is not None:
        print(f"  fetch-sandbox: {project} / PR #{pr_id}")
    elif commit:
        print(f"  fetch-sandbox: {project} / commit {commit[:12]}")
    else:
        print(f"  fetch-sandbox: {project} / {branch}")
    print(f"{'=' * 64}")

    # Resolve PR id → branch (and display base for context)
    if pr_id is not None:
        branch, _base_ref = _resolve_pr(api_base, gitea_admin_token, project, pr_id)

    # Fetch into a temporary ref, clean up afterward
    if commit:
        ref = f"refs/sandbox-fetch/commit-{commit[:12]}"
        print(f"\nFetching commit {commit} from {sandbox_url}...")
        r = git(repo_path, "fetch", sandbox_url, f"+{commit}:{ref}", check=False)
        if r.returncode != 0:
            print(f"Error: cannot fetch commit {commit} from {sandbox_url}", file=sys.stderr)
            print("\nGitea must have uploadpack.allowAnySHA1InWant=true for arbitrary-SHA fetch.")
            print(f"git stderr:\n{r.stderr}")
            sys.exit(1)
    else:
        ref = f"refs/sandbox-fetch/{branch}"
        print(f"\nFetching '{branch}' from {sandbox_url}...")
        r = git(repo_path, "fetch", sandbox_url, f"{branch}:{ref}", check=False)
        if r.returncode != 0:
            print(f"Error: Branch '{branch}' not found at {sandbox_url}", file=sys.stderr)
            print("\nAvailable branches:")
            r = git(repo_path, "ls-remote", "--heads", sandbox_url, check=False)
            if r.returncode == 0:
                for line in r.stdout.splitlines():
                    parts = line.split("\t")
                    if len(parts) == 2 and parts[1].startswith("refs/heads/"):
                        print(f"  {parts[1].removeprefix('refs/heads/')}")
            sys.exit(1)

    try:
        _run_review_and_merge(repo_path, ref, skip_review)
    finally:
        git(repo_path, "update-ref", "-d", ref, check=False)


if __name__ == "__main__":
    main()
