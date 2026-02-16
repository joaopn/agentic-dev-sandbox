#!/usr/bin/env bash
# entrypoint.sh — Agent container entrypoint.
#
# Expected environment variables:
#   GITEA_URL        — Internal Gitea URL (e.g., http://sandbox-gitea:3000)
#   GITEA_TOKEN      — Per-project Gitea API token
#   GITEA_USER       — Per-project Gitea user (e.g., agent-myproject)
#   REPO_NAME        — Repository name
#   REPO_BRANCH      — Branch to check out (optional, defaults to repo default)
#   INSTALL_CLAUDE   — If "1", install Claude Code CLI and auto-start it
#   ANTHROPIC_API_KEY — API key for Claude Code (if INSTALL_CLAUDE=1)
#   SSH_PASSWORD     — Password for SSH access
#   HTTP_PROXY       — Proxy URL for locked egress (optional)
#   HTTPS_PROXY      — Proxy URL for locked egress (optional)
#   NO_PROXY         — Proxy bypass list (optional)

set -euo pipefail

echo "=== Agent container starting ==="

# --- Fix workspace ownership (bind mounts override Dockerfile chown) ---
sudo chown agent:agent /workspace

# --- SSH setup ---
if [[ -n "${SSH_PASSWORD:-}" ]]; then
    echo "agent:${SSH_PASSWORD}" | sudo chpasswd
fi

# Import authorized keys from mounted volume if present
if [[ -f /workspace/.ssh/authorized_keys ]]; then
    mkdir -p ~/.ssh
    cp /workspace/.ssh/authorized_keys ~/.ssh/authorized_keys
    chmod 700 ~/.ssh
    chmod 600 ~/.ssh/authorized_keys
fi

sudo /usr/sbin/sshd

# --- Configure apt to use proxy ---
if [[ -n "${HTTP_PROXY:-}" ]]; then
    sudo tee /etc/apt/apt.conf.d/01proxy > /dev/null <<APT_EOF
Acquire::http::Proxy "${HTTP_PROXY}";
Acquire::https::Proxy "${HTTPS_PROXY:-$HTTP_PROXY}";
APT_EOF
fi

# --- Git configuration ---
git config --global user.name "sandbox-agent"
git config --global user.email "agent@sandbox.local"
git config --global push.default current
git config --global credential.helper store

# Store Gitea credentials for git push/pull
GITEA_HOST=$(echo "$GITEA_URL" | sed 's|https\?://||')
echo "${GITEA_URL//:\/\//:\/\/${GITEA_USER}:${GITEA_TOKEN}@}" > ~/.git-credentials
chmod 600 ~/.git-credentials
git config --global credential.helper 'store --file ~/.git-credentials'

# --- Clone or update repo ---
REPO_DIR="/workspace/${REPO_NAME}"

if [[ ! -d "${REPO_DIR}/.git" ]]; then
    echo "Cloning ${REPO_NAME} from Gitea..."
    git clone "${GITEA_URL}/${GITEA_USER}/${REPO_NAME}.git" "${REPO_DIR}"
else
    echo "Repo already cloned, pulling latest..."
    git -C "${REPO_DIR}" pull --ff-only || true
fi

cd "${REPO_DIR}"

# Checkout specified branch if set
if [[ -n "${REPO_BRANCH:-}" ]]; then
    git checkout "${REPO_BRANCH}" 2>/dev/null || git checkout -b "${REPO_BRANCH}"
fi

# --- Copy CLAUDE.md into workspace root ---
if [[ -f /workspace/.sandbox/CLAUDE.md ]]; then
    cp /workspace/.sandbox/CLAUDE.md "${REPO_DIR}/CLAUDE.md"
    echo "CLAUDE.md copied to workspace root"
fi

# --- Install Claude Code CLI if requested ---
if [[ "${INSTALL_CLAUDE:-0}" == "1" ]]; then
    echo "Installing Claude Code CLI..."
    sudo npm install -g @anthropic-ai/claude-code 2>&1 | tail -1
    echo "Claude Code installed"
fi

# --- Start byobu session ---
echo "Starting byobu session 'main'..."

BYOBU_CMD="cd ${REPO_DIR}"
if [[ "${INSTALL_CLAUDE:-0}" == "1" ]]; then
    BYOBU_CMD="${BYOBU_CMD} && claude"
fi

byobu new-session -d -s main -c "${REPO_DIR}" "${BYOBU_CMD}; exec bash"

echo "=== Agent container ready ==="
echo "Repo: ${REPO_DIR}"
echo "Byobu session: main"

# Keep the container running
exec tail -f /dev/null
