#!/usr/bin/env python3
"""
review-server.py — Gitea webhook listener that posts LLM-generated security reviews.

Receives issue_comment webhooks from Gitea and dispatches slash commands.
Currently supports /security — fetches the full PR diff, sends it to an LLM
for security-focused review, and posts findings as a PR comment.

Supports multiple LLM providers via a unified interface:
  - anthropic:   Anthropic Messages API (default)
  - openai:      OpenAI Chat Completions API
  - openrouter:  OpenRouter (OpenAI-compatible)
  - local:       Any OpenAI-compatible local server (e.g., vLLM, llama.cpp)

Configuration is split between env vars (secrets, runtime) and review-config.yaml
(per-command prompts, provider endpoints). Model is always set via REVIEWER_MODEL env var.

Environment variables:
    REVIEWER_PROVIDER — LLM provider: anthropic, openai, openrouter, local
    REVIEWER_API_KEY  — API key for the chosen provider
    REVIEWER_MODEL    — Model name (required)
    REVIEWER_ENDPOINT — Custom API endpoint (overrides config, required for local)
    GITEA_URL         — Internal Gitea URL (e.g., http://sandbox-gitea:3000)
    BOT_SECURITY_TOKEN — Gitea token for bot-security user (reading diffs + posting comments)
"""

import json
import logging
import os
import sys
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import URLError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("review-server")

# ─── Config loading ───────────────────────────────────────────────────────────

CONFIG_PATH = Path(__file__).resolve().parent / "review-config.yaml"


def load_yaml_config(path: Path) -> dict:
    """Minimal YAML loader for our flat config (no external deps).

    Handles:
      - top-level scalar keys (key: value)
      - top-level mapping keys with indented children (providers:)
      - nested block scalars within mappings (prompts: security: |)
      - top-level block scalars (key: |)
      - comments (#)
    """
    config: dict = {}
    if not path.exists():
        return config

    lines = path.read_text().splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # Skip blanks and comments
        if not stripped or stripped.startswith("#"):
            i += 1
            continue

        # Must be a top-level key (no leading whitespace)
        if line[0] != " " and ":" in stripped:
            key, _, value = stripped.partition(":")
            key = key.strip()
            value = value.strip()

            if value == "|":
                # Block scalar — collect indented lines
                block_lines = []
                i += 1
                while i < len(lines):
                    bline = lines[i]
                    if bline and not bline[0].isspace():
                        break
                    block_lines.append(bline)
                    i += 1
                # Dedent: find minimum indentation of non-empty lines
                non_empty = [l for l in block_lines if l.strip()]
                if non_empty:
                    indent = min(len(l) - len(l.lstrip()) for l in non_empty)
                    block_lines = [l[indent:] if len(l) > indent else "" for l in block_lines]
                config[key] = "\n".join(block_lines).rstrip("\n") + "\n"
                continue

            if not value:
                # Mapping — collect indented key: value pairs (including nested block scalars)
                mapping = {}
                i += 1
                while i < len(lines):
                    mline = lines[i]
                    mstripped = mline.strip()
                    if not mstripped or mstripped.startswith("#"):
                        i += 1
                        continue
                    if mline[0] != " ":
                        break
                    if ":" in mstripped:
                        mkey, _, mval = mstripped.partition(":")
                        mkey = mkey.strip()
                        mval = mval.strip()
                        if mval == "|":
                            # Nested block scalar
                            block_lines = []
                            base_indent = len(mline) - len(mline.lstrip())
                            i += 1
                            while i < len(lines):
                                bline = lines[i]
                                if bline.strip() and not bline[0].isspace():
                                    break
                                # Stop if line is at same or lower indent (sibling/parent key)
                                if bline.strip() and (len(bline) - len(bline.lstrip())) <= base_indent:
                                    break
                                block_lines.append(bline)
                                i += 1
                            non_empty = [bl for bl in block_lines if bl.strip()]
                            if non_empty:
                                indent = min(len(bl) - len(bl.lstrip()) for bl in non_empty)
                                block_lines = [bl[indent:] if len(bl) > indent else "" for bl in block_lines]
                            mapping[mkey] = "\n".join(block_lines).rstrip("\n") + "\n"
                            continue
                        else:
                            mapping[mkey] = mval
                    i += 1
                config[key] = mapping
                continue

            # Plain scalar
            config[key] = value
        i += 1

    return config


YAML_CONFIG = load_yaml_config(CONFIG_PATH)

# ─── Resolved configuration ──────────────────────────────────────────────────

GITEA_URL = os.environ.get("GITEA_URL", "http://sandbox-gitea:3000")
GITEA_TOKEN = os.environ.get("BOT_SECURITY_TOKEN", "")
REVIEWER_PROVIDER = os.environ.get("REVIEWER_PROVIDER", "anthropic").lower()
REVIEWER_API_KEY = os.environ.get("REVIEWER_API_KEY", "")
REVIEWER_MODEL = os.environ.get("REVIEWER_MODEL", "")
REVIEWER_ENDPOINT = os.environ.get("REVIEWER_ENDPOINT", "")
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "8080"))

# From yaml
PROVIDER_ENDPOINTS: dict = YAML_CONFIG.get("providers", {})
PROMPTS: dict = YAML_CONFIG.get("prompts", {})
DEFAULT_PROMPT: str = "Review this diff for security issues:\n\n{diff}\n"
MAX_DIFF_SIZE: int = int(YAML_CONFIG.get("max_diff_size", 100_000))
MAX_TOKENS: int = int(YAML_CONFIG.get("max_tokens", 4096))


# ─── LLM provider implementations ────────────────────────────────────────────


def get_endpoint() -> str:
    """Resolve the API endpoint for the configured provider."""
    if REVIEWER_ENDPOINT:
        return REVIEWER_ENDPOINT.rstrip("/")
    endpoint = PROVIDER_ENDPOINTS.get(REVIEWER_PROVIDER, "")
    if not endpoint:
        log.error("REVIEWER_ENDPOINT is required for provider '%s'", REVIEWER_PROVIDER)
        return ""
    return endpoint


def call_anthropic(prompt: str) -> str:
    """Call the Anthropic Messages API."""
    endpoint = get_endpoint()
    if not endpoint or not REVIEWER_MODEL:
        return "*Review skipped: missing endpoint or model configuration.*"

    body = json.dumps({
        "model": REVIEWER_MODEL,
        "max_tokens": MAX_TOKENS,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()

    headers = {
        "x-api-key": REVIEWER_API_KEY,
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
    except URLError as e:
        log.error("Anthropic API error: %s", e)
        return f"*Review failed: {e}*"


def call_openai_compatible(prompt: str) -> str:
    """Call an OpenAI-compatible Chat Completions API.

    Works for openai, openrouter, and local providers.
    """
    endpoint = get_endpoint()
    if not endpoint or not REVIEWER_MODEL:
        return "*Review skipped: missing endpoint or model configuration.*"

    body = json.dumps({
        "model": REVIEWER_MODEL,
        "max_tokens": MAX_TOKENS,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()

    headers = {
        "content-type": "application/json",
    }
    if REVIEWER_API_KEY:
        headers["authorization"] = f"Bearer {REVIEWER_API_KEY}"

    req = Request(
        f"{endpoint}/v1/chat/completions",
        data=body,
        headers=headers,
        method="POST",
    )

    try:
        with urlopen(req, timeout=120) as resp:
            result = json.loads(resp.read().decode())
            choices = result.get("choices", [])
            if choices:
                return choices[0].get("message", {}).get("content", "*Empty response.*")
            return "*Review returned empty response.*"
    except URLError as e:
        log.error("%s API error: %s", REVIEWER_PROVIDER, e)
        return f"*Review failed: {e}*"


PROVIDERS = {
    "anthropic": call_anthropic,
    "openai": call_openai_compatible,
    "openrouter": call_openai_compatible,
    "local": call_openai_compatible,
}


def call_llm(command: str, diff: str) -> str:
    """Send the diff to the configured LLM using the prompt for the given command."""
    if not REVIEWER_API_KEY and REVIEWER_PROVIDER != "local":
        return "*Review skipped: REVIEWER_API_KEY not configured.*"

    provider_fn = PROVIDERS.get(REVIEWER_PROVIDER)
    if not provider_fn:
        return f"*Review skipped: unknown provider '{REVIEWER_PROVIDER}'.*"

    prompt_template = PROMPTS.get(command, DEFAULT_PROMPT)
    prompt = prompt_template.format(diff=diff[:MAX_DIFF_SIZE])
    return provider_fn(prompt)


# ─── Gitea helpers ────────────────────────────────────────────────────────────


def gitea_api(method: str, path: str, body: dict | None = None) -> dict | str:
    """Call the Gitea API."""
    url = f"{GITEA_URL}/api/v1{path}"
    headers = {
        "Authorization": f"token {GITEA_TOKEN}",
        "Content-Type": "application/json",
    }
    data = json.dumps(body).encode() if body else None
    req = Request(url, data=data, headers=headers, method=method)
    try:
        with urlopen(req, timeout=30) as resp:
            content = resp.read().decode()
            if resp.headers.get("Content-Type", "").startswith("application/json"):
                return json.loads(content)
            return content
    except URLError as e:
        log.error("Gitea API error: %s %s -> %s", method, path, e)
        raise


def get_pr_diff(owner: str, repo: str, pr_number: int) -> str:
    """Fetch the full diff for a pull request from Gitea."""
    url = f"{GITEA_URL}/api/v1/repos/{owner}/{repo}/pulls/{pr_number}.diff"
    headers = {"Authorization": f"token {GITEA_TOKEN}"}
    req = Request(url, headers=headers)
    try:
        with urlopen(req, timeout=30) as resp:
            return resp.read().decode()
    except URLError as e:
        log.error("Failed to fetch PR #%d diff: %s", pr_number, e)
        return ""


REVIEW_MARKER = "<!-- automated-security-review -->"


def has_existing_review(owner: str, repo: str, pr_number: int, sha: str) -> bool:
    """Check if a review comment already exists for this commit SHA on the PR."""
    try:
        comments = gitea_api("GET", f"/repos/{owner}/{repo}/issues/{pr_number}/comments")
        if isinstance(comments, list):
            return any(
                REVIEW_MARKER in c.get("body", "") and sha[:12] in c.get("body", "")
                for c in comments
            )
    except URLError:
        pass
    return False


def post_review_comment(owner: str, repo: str, pr_number: int, sha: str, body: str) -> None:
    """Post a review as a PR comment. Includes a hidden marker so repo-watch can ignore it."""
    comment_body = f"{REVIEW_MARKER}\n## Security Review (automated)\n**Commit:** `{sha[:12]}`\n\n{body}"
    gitea_api("POST", f"/repos/{owner}/{repo}/issues/{pr_number}/comments", {
        "body": comment_body,
    })
    log.info("Posted review comment on %s/%s PR #%d (%s)", owner, repo, pr_number, sha[:8])


# ─── Webhook handler ─────────────────────────────────────────────────────────


def review_pr(owner: str, repo: str, pr_number: int, branch: str, head_sha: str,
              command: str = "security") -> None:
    """Fetch diff, call LLM, and post review comment for a PR."""
    # Dedup: skip if we already reviewed this exact commit
    if has_existing_review(owner, repo, pr_number, head_sha):
        log.info("PR %s/%s#%d: already reviewed %s, skipping", owner, repo, pr_number, head_sha[:8])
        return

    log.info("Reviewing PR %s/%s#%d (branch %s, HEAD %s)",
             owner, repo, pr_number, branch, head_sha[:8])

    diff = get_pr_diff(owner, repo, pr_number)
    if not diff:
        log.warning("Empty diff for PR %s/%s#%d, skipping review", owner, repo, pr_number)
        return

    log.info("Sending PR diff (%d bytes) to %s (%s) for /%s...",
             len(diff), REVIEWER_PROVIDER, REVIEWER_MODEL, command)
    review = call_llm(command, diff)
    post_review_comment(owner, repo, pr_number, head_sha, review)


def cmd_security(owner: str, repo: str, issue_number: int) -> None:
    """Handle /security command — run a security review on the PR."""
    try:
        pr = gitea_api("GET", f"/repos/{owner}/{repo}/pulls/{issue_number}")
    except URLError:
        log.error("Not a PR or fetch failed: %s/%s#%d", owner, repo, issue_number)
        return

    branch = pr.get("head", {}).get("ref", "")
    head_sha = pr.get("head", {}).get("sha", "")
    if not head_sha:
        log.warning("PR %s/%s#%d: no HEAD SHA, skipping", owner, repo, issue_number)
        return

    review_pr(owner, repo, issue_number, branch, head_sha, command="security")


# Command dispatch — add new /commands here
COMMANDS: dict[str, callable] = {
    "/security": cmd_security,
}


def handle_comment_webhook(payload: dict) -> None:
    """Process a Gitea issue_comment webhook — dispatches /commands."""
    action = payload.get("action", "")
    if action != "created":
        return

    comment = payload.get("comment", {})
    body = comment.get("body", "").strip()

    # Match the first word against known commands
    cmd = body.split()[0] if body else ""
    handler = COMMANDS.get(cmd)
    if not handler:
        return

    repo_full = payload.get("repository", {}).get("full_name", "")
    issue_number = payload.get("issue", {}).get("number")
    owner, repo = repo_full.split("/", 1)

    log.info("Command '%s' on %s#%d by %s",
             cmd, repo_full, issue_number, comment.get("user", {}).get("login"))
    handler(owner, repo, issue_number)


class WebhookHandler(BaseHTTPRequestHandler):
    """HTTP handler for Gitea webhooks."""

    def do_POST(self):
        if self.path != "/webhook":
            self.send_response(404)
            self.end_headers()
            return

        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)

        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            log.error("Invalid JSON in webhook payload")
            self.send_response(400)
            self.end_headers()
            return

        # Respond immediately (process async would be better, but keep it simple)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"status":"ok"}')

        # Process the webhook
        event = self.headers.get("X-Gitea-Event", "")
        try:
            if event in ("issue_comment", "pull_request_comment"):
                handle_comment_webhook(payload)
            else:
                log.info("Ignoring event type: %s", event)
        except Exception:
            log.exception("Error processing %s webhook", event)

    def do_GET(self):
        """Health check endpoint."""
        if self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status":"healthy"}')
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        """Override to use our logger."""
        log.info(format, *args)


def main():
    if REVIEWER_PROVIDER not in PROVIDERS:
        log.error("Unknown REVIEWER_PROVIDER '%s'. Valid: %s",
                  REVIEWER_PROVIDER, ", ".join(PROVIDERS))
        sys.exit(1)

    if not REVIEWER_MODEL:
        log.error("REVIEWER_MODEL is required")
        sys.exit(1)

    if not GITEA_TOKEN:
        log.warning("BOT_SECURITY_TOKEN not set — cannot read diffs or post comments")
    if not REVIEWER_API_KEY and REVIEWER_PROVIDER != "local":
        log.warning("REVIEWER_API_KEY not set — reviews will be skipped")

    if not get_endpoint():
        log.error("No endpoint configured for provider '%s'. "
                  "Set REVIEWER_ENDPOINT or add it to review-config.yaml", REVIEWER_PROVIDER)
        sys.exit(1)

    log.info("Provider: %s | Model: %s | Endpoint: %s",
             REVIEWER_PROVIDER, REVIEWER_MODEL, get_endpoint())

    server = HTTPServer(("0.0.0.0", LISTEN_PORT), WebhookHandler)
    log.info("Review server listening on port %d", LISTEN_PORT)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Shutting down")
        server.shutdown()


if __name__ == "__main__":
    main()
