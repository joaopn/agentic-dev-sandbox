#!/usr/bin/env python3
"""
review-server.py — Gitea webhook listener that posts LLM-generated security reviews.

Receives push webhooks from Gitea, fetches the diff, sends it to an LLM for
security-focused review, and posts findings as commit comments on Gitea.

Supports multiple LLM providers via a unified interface:
  - anthropic:   Anthropic Messages API (default)
  - openai:      OpenAI Chat Completions API
  - openrouter:  OpenRouter (OpenAI-compatible)
  - local:       Any OpenAI-compatible local server (e.g., vLLM, llama.cpp)

Configuration is split between env vars (secrets, runtime) and review-config.yaml
(prompt, provider endpoints). Model is always set via REVIEWER_MODEL env var.

Environment variables:
    REVIEWER_PROVIDER — LLM provider: anthropic, openai, openrouter, local
    REVIEWER_API_KEY  — API key for the chosen provider
    REVIEWER_MODEL    — Model name (required)
    REVIEWER_ENDPOINT — Custom API endpoint (overrides config, required for local)
    GITEA_URL         — Internal Gitea URL (e.g., http://sandbox-gitea:3000)
    GITEA_ADMIN_TOKEN — Gitea admin API token (for reading diffs and posting comments)
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
      - multi-line block scalars (prompt: |)
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
                # Mapping — collect indented key: value pairs
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
                        mapping[mkey.strip()] = mval.strip()
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
GITEA_TOKEN = os.environ.get("GITEA_ADMIN_TOKEN", "")
REVIEWER_PROVIDER = os.environ.get("REVIEWER_PROVIDER", "anthropic").lower()
REVIEWER_API_KEY = os.environ.get("REVIEWER_API_KEY", "")
REVIEWER_MODEL = os.environ.get("REVIEWER_MODEL", "")
REVIEWER_ENDPOINT = os.environ.get("REVIEWER_ENDPOINT", "")
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "8080"))

# From yaml
PROVIDER_ENDPOINTS: dict = YAML_CONFIG.get("providers", {})
REVIEW_PROMPT: str = YAML_CONFIG.get("prompt", "Review this diff for security issues:\n\n{diff}\n")
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


def call_llm(diff: str) -> str:
    """Send the diff to the configured LLM for security review."""
    if not REVIEWER_API_KEY and REVIEWER_PROVIDER != "local":
        return "*Review skipped: REVIEWER_API_KEY not configured.*"

    provider_fn = PROVIDERS.get(REVIEWER_PROVIDER)
    if not provider_fn:
        return f"*Review skipped: unknown provider '{REVIEWER_PROVIDER}'.*"

    prompt = REVIEW_PROMPT.format(diff=diff[:MAX_DIFF_SIZE])
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


def get_commit_diff(owner: str, repo: str, sha: str) -> str:
    """Fetch the diff for a specific commit from Gitea."""
    url = f"{GITEA_URL}/api/v1/repos/{owner}/{repo}/git/commits/{sha}.diff"
    headers = {"Authorization": f"token {GITEA_TOKEN}"}
    req = Request(url, headers=headers)
    try:
        with urlopen(req, timeout=30) as resp:
            return resp.read().decode()
    except URLError as e:
        log.error("Failed to fetch diff for %s: %s", sha, e)
        return ""


def post_commit_comment(owner: str, repo: str, sha: str, body: str) -> None:
    """Post a review comment on a Gitea commit."""
    comment_body = f"## Security Review (automated)\n\n{body}"
    gitea_api("POST", f"/repos/{owner}/{repo}/git/commits/{sha}/comments", {
        "body": comment_body,
    })
    log.info("Posted review comment on %s/%s@%s", owner, repo, sha[:8])


# ─── Webhook handler ─────────────────────────────────────────────────────────


def handle_push_webhook(payload: dict) -> None:
    """Process a Gitea push webhook."""
    repo_full = payload.get("repository", {}).get("full_name", "")
    ref = payload.get("ref", "")
    commits = payload.get("commits", [])

    if not repo_full or not commits:
        log.info("Ignoring push with no commits: %s", repo_full)
        return

    # Only review agent/* branches
    branch = ref.replace("refs/heads/", "")
    if not branch.startswith("agent/"):
        log.info("Skipping non-agent branch: %s", branch)
        return

    owner, repo = repo_full.split("/", 1)
    log.info("Reviewing push to %s/%s branch %s (%d commits)",
             owner, repo, branch, len(commits))

    # Review the latest commit (which contains the cumulative diff for a push)
    latest_sha = commits[-1]["id"]
    diff = get_commit_diff(owner, repo, latest_sha)

    if not diff:
        log.warning("Empty diff for %s@%s, skipping review", repo_full, latest_sha[:8])
        return

    log.info("Sending diff (%d bytes) to %s (%s) for review...",
             len(diff), REVIEWER_PROVIDER, REVIEWER_MODEL)
    review = call_llm(diff)
    post_commit_comment(owner, repo, latest_sha, review)


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
        if event == "push":
            try:
                handle_push_webhook(payload)
            except Exception:
                log.exception("Error processing push webhook")
        else:
            log.info("Ignoring event type: %s", event)

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
        log.warning("GITEA_ADMIN_TOKEN not set — cannot read diffs or post comments")
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
