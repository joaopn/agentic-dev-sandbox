#!/usr/bin/env python3
"""fetch-sandbox — Fetch agent branch from sandbox Gitea, run security review and safety checks.

Usage:
    python fetch-sandbox.py <repo_path> <branch> [--remote <name>] [--base <branch>] [--skip-review]
    python fetch-sandbox.py setup

    repo_path      Path to your local git repository
    branch         Branch name to fetch (e.g. agent/feature-branch, main)
    --remote       Use a pre-configured git remote instead of fetching by URL
    --base         Override base branch for diff computation (default: auto-detect)
    --skip-review  Skip the LLM security review step
    setup          Configure LLM provider for security reviews (interactive)
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

    prompt = prompt_template.format(diff=diff[:max_diff_size])
    return provider_fn(prompt, api_key, model, endpoint, max_tokens)


def review_diff(repo_path: str, ref: str, base_branch: str) -> bool:
    """Compute git diff, send to LLM, display review. Returns True if safe to proceed."""
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

    # Load prompt and tunables from review-config.yaml
    yaml_cfg = load_yaml_config(SCRIPT_DIR / "review-config.yaml")
    prompt_template = yaml_cfg.get("prompt", DEFAULT_PROMPT)
    max_diff_size = int(yaml_cfg.get("max_diff_size", 100_000))
    max_tokens = int(yaml_cfg.get("max_tokens", 4096))

    # Compute diff
    r = git(repo_path, "diff", f"{base_branch}...{ref}", check=False)
    diff = r.stdout
    if not diff:
        print("  (No diff to review — branch matches base.)")
        return True

    print(f"  Provider: {provider} | Model: {model}")
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


def run_safety_checks(repo_path: str, ref: str, base_override: str = "") -> str:
    """Check for symlinks and auto-execute file modifications. Returns base_branch."""
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

    # Determine base branch: explicit override > origin/HEAD > "main" fallback
    if base_override:
        base_branch = base_override
    else:
        r = git(repo_path, "symbolic-ref", "refs/remotes/origin/HEAD", check=False)
        base_branch = r.stdout.strip().replace("refs/remotes/origin/", "") if r.returncode == 0 else "main"
    print(f"  Base branch: {base_branch}")

    r = git(repo_path, "diff", "--quiet", f"{base_branch}...{ref}", "--", *AUTO_EXEC_PATHS, check=False)
    if r.returncode != 0:
        print("  \u26a0  Auto-execute files: MODIFIED")
    else:
        print("  Auto-execute files: unchanged")

    return base_branch


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

    # Write to .env
    print("Saving configuration...")
    update_env_key("REVIEWER_PROVIDER", provider)
    update_env_key("REVIEWER_API_KEY", api_key)
    update_env_key("REVIEWER_MODEL", model)
    if endpoint:
        update_env_key("REVIEWER_ENDPOINT", endpoint)

    print(f"""
=== Reviewer Ready ===
Provider:  {provider}
Model:     {model}
Reviews will run automatically when you use fetch-sandbox.py.""")


# ─── Main ─────────────────────────────────────────────────────────────────────


def _run_review_and_merge(repo_path: str, ref: str,
                          base_override: str, skip_review: bool) -> None:
    """Run safety checks, optional LLM review, and squash-merge."""
    # Safety checks
    base_branch = run_safety_checks(repo_path, ref, base_override)

    # LLM security review
    if not skip_review:
        safe = review_diff(repo_path, ref, base_branch)
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


def main() -> None:
    # Dispatch subcommand
    if len(sys.argv) >= 2 and sys.argv[1] == "setup":
        cmd_setup()
        return

    skip_review = "--skip-review" in sys.argv
    base_override = ""
    remote_name = ""
    argv = []
    i = 1
    while i < len(sys.argv):
        if sys.argv[i] == "--skip-review":
            i += 1
            continue
        if sys.argv[i] == "--base" and i + 1 < len(sys.argv):
            base_override = sys.argv[i + 1]
            i += 2
            continue
        if sys.argv[i] == "--remote" and i + 1 < len(sys.argv):
            remote_name = sys.argv[i + 1]
            i += 2
            continue
        argv.append(sys.argv[i])
        i += 1

    if len(argv) < 2:
        print("Usage: python fetch-sandbox.py <repo_path> <branch> [--remote <name>] [--base <branch>] [--skip-review]")
        print("       python fetch-sandbox.py setup")
        print()
        print("  repo_path      Path to your local git repository")
        print("  branch         Branch name to fetch (e.g. agent/feature-branch, main)")
        print("  --remote       Use a pre-configured git remote instead of fetching by URL")
        print("  --base         Override base branch for diff computation (default: auto-detect)")
        print("  --skip-review  Skip the LLM security review step")
        print("  setup          Configure LLM provider for security reviews")
        sys.exit(1)

    repo_path = os.path.abspath(argv[0])
    branch = argv[1]

    project = os.path.basename(repo_path)
    gitea_user = f"agent-{project}"

    # Validate git repo
    r = subprocess.run(["git", "-C", repo_path, "rev-parse", "--is-inside-work-tree"],
                       capture_output=True, text=True, check=False)
    if r.returncode != 0:
        die(f"Not a git repository: {repo_path}")

    # Load config
    gitea_port, _ = load_env()

    # Compute fetch URL and ref
    sandbox_url = f"http://localhost:{gitea_port}/{gitea_user}/{project}.git"
    if remote_name:
        ref = f"{remote_name}/{branch}"
    else:
        ref = f"refs/sandbox-fetch/{branch}"

    print(f"{'=' * 64}")
    print(f"  fetch-sandbox: {project} / {branch}")
    print(f"{'=' * 64}")

    # ── Fetch and merge ──
    if remote_name:
        # --remote mode: use a pre-configured remote
        print(f"\nFetching from remote '{remote_name}'...")
        r = git(repo_path, "fetch", remote_name, check=False)
        if r.returncode != 0:
            die(f"git fetch {remote_name} failed:\n{r.stderr}")

        r = git(repo_path, "rev-parse", ref, check=False)
        if r.returncode != 0:
            print(f"Error: Branch '{branch}' not found on remote '{remote_name}'.", file=sys.stderr)
            print(f"\nAvailable branches on '{remote_name}':")
            r = git(repo_path, "branch", "-r", check=False)
            for line in r.stdout.splitlines():
                line = line.strip()
                if line.startswith(f"{remote_name}/"):
                    print(f"  {line.removeprefix(f'{remote_name}/')}")
            sys.exit(1)

        _run_review_and_merge(repo_path, ref, base_override, skip_review)

    else:
        # URL-fetch mode: fetch into a temporary ref, clean up afterward
        print(f"\nFetching '{branch}' from {sandbox_url}...")
        r = git(repo_path, "fetch", sandbox_url,
                f"{branch}:{ref}", check=False)
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
            _run_review_and_merge(repo_path, ref, base_override, skip_review)
        finally:
            git(repo_path, "update-ref", "-d", ref, check=False)


if __name__ == "__main__":
    main()
