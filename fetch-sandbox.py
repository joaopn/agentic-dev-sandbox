#!/usr/bin/env python3
"""fetch-sandbox — Fetch agent branch from sandbox Gitea, show security review and safety checks.

Usage:
    python fetch-sandbox.py <repo_path> <branch>

    repo_path  Path to your local git repository
    branch     Branch name on the staging remote (e.g. agent/feature-branch, main)
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

REVIEW_MARKER = "<!-- automated-security-review -->"
NO_ISSUES_TEXT = "**No security concerns found.**"
BOT_USER = "bot-security"

AUTO_EXEC_PATHS = [
    ".envrc", ".vscode/", ".husky/", ".pre-commit-config.yaml", ".gitmodules",
    "package.json", "setup.py", "setup.cfg", "Makefile", "CMakeLists.txt",
    ".cargo/config.toml", ".eslintrc.js", ".prettierrc.js",
]


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


# ─── Security review ─────────────────────────────────────────────────────────


def fetch_security_reviews(api_base: str, token: str, gitea_user: str,
                           project: str, branch: str) -> None:
    """Find PRs for the branch and display security review comments."""
    print("\n── Security Review (automated) ──")

    # Paginate all PRs, filter by head branch
    matching_prs: list[dict] = []
    page = 1
    page_size = 50
    while True:
        try:
            prs = gitea_get(api_base, token,
                            f"/repos/{gitea_user}/{project}/pulls?state=all&page={page}&limit={page_size}")
        except (HTTPError, URLError) as e:
            print(f"  (Could not reach Gitea API: {e})")
            return
        if not isinstance(prs, list):
            break
        for pr in prs:
            if pr.get("head", {}).get("ref") == branch:
                matching_prs.append(pr)
        if len(prs) < page_size:
            break
        page += 1

    # Collect security review comments from matching PRs
    review_bodies: list[str] = []
    for pr in matching_prs:
        pr_number = pr["number"]
        try:
            comments = gitea_get(api_base, token,
                                 f"/repos/{gitea_user}/{project}/issues/{pr_number}/comments")
        except (HTTPError, URLError):
            continue
        if not isinstance(comments, list):
            continue
        for c in comments:
            body = c.get("body", "")
            user = c.get("user", {}).get("login", "")
            if REVIEW_MARKER in body and user == BOT_USER:
                review_bodies.append(body)

    print(f"  Found {len(matching_prs)} PR(s), {len(review_bodies)} security review(s).")

    if not review_bodies:
        print("  (No automated security review found for this branch)")
    elif all(NO_ISSUES_TEXT in b and "### [SEVERITY:" not in b for b in review_bodies):
        print("  No security issues found.")
    else:
        print()
        for body in review_bodies:
            print(body)
            print()


# ─── Safety checks ────────────────────────────────────────────────────────────


def run_safety_checks(repo_path: str, ref: str) -> None:
    """Check for symlinks and auto-execute file modifications."""
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

    # Auto-execute files
    r = git(repo_path, "symbolic-ref", "refs/remotes/origin/HEAD", check=False)
    base_branch = r.stdout.strip().replace("refs/remotes/origin/", "") if r.returncode == 0 else "main"

    r = git(repo_path, "diff", "--quiet", f"{base_branch}...{ref}", "--", *AUTO_EXEC_PATHS, check=False)
    if r.returncode != 0:
        print("  \u26a0  Auto-execute files: MODIFIED")
    else:
        print("  Auto-execute files: unchanged")

    return base_branch


# ─── Main ─────────────────────────────────────────────────────────────────────


def main() -> None:
    if len(sys.argv) < 3:
        print("Usage: python fetch-sandbox.py <repo_path> <branch>")
        print()
        print("  repo_path  Path to your local git repository")
        print("  branch     Branch name on the staging remote (e.g. agent/feature-branch, main)")
        sys.exit(1)

    repo_path = os.path.abspath(sys.argv[1])
    branch = sys.argv[2]

    project = os.path.basename(repo_path)
    gitea_user = f"agent-{project}"
    ref = f"staging/{branch}"

    # Validate git repo
    r = subprocess.run(["git", "-C", repo_path, "rev-parse", "--is-inside-work-tree"],
                       capture_output=True, text=True, check=False)
    if r.returncode != 0:
        die(f"Not a git repository: {repo_path}")

    # Load config
    gitea_port, gitea_admin_token = load_env()
    api_base = f"http://localhost:{gitea_port}/api/v1"

    print(f"{'=' * 64}")
    print(f"  fetch-sandbox: {project} / {branch}")
    print(f"{'=' * 64}")

    # ── Step 1: Security review (API only) ──
    fetch_security_reviews(api_base, gitea_admin_token, gitea_user, project, branch)

    # ── Step 2: Staging remote + fetch ──
    r = git(repo_path, "remote", check=False)
    has_staging = "staging" in r.stdout.splitlines()

    staging_url = f"http://localhost:{gitea_port}/{gitea_user}/{project}.git"

    if not has_staging:
        print(f"\nThe 'staging' remote is not configured.")
        print(f"  URL: {staging_url}")
        answer = input("\nAdd staging remote? [Y/n] ").strip() or "Y"
        if answer.lower() not in ("y", "yes"):
            print("Stopped. Security review info shown above.")
            return
        git(repo_path, "remote", "add", "staging", staging_url)
        print("Added 'staging' remote.")

    print("\nFetching from staging...")
    r = git(repo_path, "fetch", "staging", check=False)
    if r.returncode != 0:
        die(f"git fetch staging failed:\n{r.stderr}")

    # Verify branch exists
    r = git(repo_path, "rev-parse", ref, check=False)
    if r.returncode != 0:
        print(f"Error: Branch {branch} not found on staging remote.", file=sys.stderr)
        print("\nAvailable staging branches:")
        r = git(repo_path, "branch", "-r", check=False)
        for line in r.stdout.splitlines():
            line = line.strip()
            if line.startswith("staging/"):
                print(f"  {line.removeprefix('staging/')}")
        sys.exit(1)

    # ── Step 3: Safety checks (git-based, needs fetched refs) ──
    base_branch = run_safety_checks(repo_path, ref)

    # ── Step 4: Instructions ──
    print(f"\n{'=' * 64}")
    print(f"""
Review on your git client, or:
  git diff {base_branch}...{ref}

Merge when ready:
  git merge --squash {ref}
  git commit
  git push origin {base_branch}""")


if __name__ == "__main__":
    main()
