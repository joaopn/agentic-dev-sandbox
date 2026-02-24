#!/usr/bin/env bash
# entrypoint.sh — Agent container entrypoint.
#
# Expected environment variables:
#   GITEA_URL        — Internal Gitea URL (e.g., http://sandbox-gitea:3000)
#   GITEA_TOKEN      — Per-project Gitea API token
#   GITEA_USER       — Per-project Gitea user (e.g., agent-myproject)
#   REPO_NAME        — Repository name
#   REPO_BRANCH      — Branch to check out (optional, defaults to repo default)
#   SSH_PASSWORD     — Password for SSH access
#   HOST_GID         — Host user's GID, for bind-mount access (optional)
#   CLAUDE_YOLO      — Install Claude Code + bypass permissions (optional)

set -euo pipefail

echo "=== Agent container starting ==="

# --- Match agent group GID to host user for bind-mount access ---
if [[ -n "${HOST_GID:-}" ]]; then
    sudo groupmod -o -g "${HOST_GID}" agent 2>/dev/null || true
fi

# --- Restore home directory dotfiles (volume mount hides image contents) ---
sudo chown agent:agent /home/agent

# Group-writable umask so host user (shared GID) gets rw on new files
umask 002
if [[ ! -f ~/.bashrc ]]; then
    cp -a /etc/agent-skel/. ~/
fi

# --- SSH setup ---
if [[ -n "${SSH_PASSWORD:-}" ]]; then
    echo "agent:${SSH_PASSWORD}" | sudo chpasswd
fi

# Fix permissions on authorized keys if provided via container/
if [[ -f ~/.ssh/authorized_keys ]]; then
    chmod 700 ~/.ssh
    chmod 600 ~/.ssh/authorized_keys
fi

sudo /usr/sbin/sshd

# --- Docker-in-Docker (start dockerd if installed, e.g. Sysbox runtime) ---
if command -v dockerd &>/dev/null; then
    sudo sh -c 'dockerd > /tmp/dockerd.log 2>&1 &'
fi

# --- Git configuration ---
git config --global user.name "sandbox-agent"
git config --global user.email "agent@sandbox.local"
git config --global push.default current
git config --global credential.helper store

# Store Gitea credentials for git push/pull
echo "${GITEA_URL//:\/\//:\/\/${GITEA_USER}:${GITEA_TOKEN}@}" > ~/.git-credentials
chmod 600 ~/.git-credentials
git config --global credential.helper 'store --file ~/.git-credentials'

# --- Persist sandbox env vars for login shells (su - agent, byobu, SSH) ---
# Docker env vars are only inherited by child processes of PID 1.  Login shells
# (su -, ssh) start clean and lose them.  /etc/profile.d/ is sourced by
# /etc/profile on every Linux distro for all login shells.
sudo mkdir -p /etc/profile.d
sudo tee /etc/profile.d/sandbox-env.sh > /dev/null << SANDBOX_ENV
export GITEA_URL="${GITEA_URL}"
export GITEA_TOKEN="${GITEA_TOKEN}"
export GITEA_USER="${GITEA_USER}"
export REPO_NAME="${REPO_NAME}"
export REPO_BRANCH="${REPO_BRANCH:-}"
export SSH_PASSWORD="${SSH_PASSWORD}"
SANDBOX_ENV

# --- Ensure umask 002 in interactive shells (for host bind-mount access) ---
if ! grep -q 'umask 002' ~/.bashrc 2>/dev/null; then
    echo 'umask 002' >> ~/.bashrc
fi

# --- Claude Code setup (if --claude-yolo) ---
if [[ "${CLAUDE_YOLO:-}" == "true" ]]; then
    # Ensure ~/.local/bin is in PATH
    if ! grep -q '.local/bin' ~/.bashrc 2>/dev/null; then
        echo "export PATH=\"\$HOME/.local/bin:\$PATH\"" >> ~/.bashrc
    fi

    # Pre-configure settings (no network needed, only on first run)
    if [[ ! -f ~/.claude/settings.json ]]; then
        mkdir -p ~/.claude
        cat > ~/.claude/settings.json << 'SETTINGS'
{
  "permissions": {
    "defaultMode": "bypassPermissions"
  },
  "theme": "dark"
}
SETTINGS
    fi

    if [[ ! -f ~/.claude.json ]]; then
        cat > ~/.claude.json << 'SETTINGS'
{
  "theme": "dark"
}
SETTINGS
    fi

fi

# --- Clone or update repo ---
REPO_DIR=~/"${REPO_NAME}"

if [[ ! -d "${REPO_DIR}/.git" ]]; then
    echo "Cloning ${REPO_NAME} from Gitea..."
    git clone "${GITEA_URL}/${GITEA_USER}/${REPO_NAME}.git" "${REPO_DIR}"
else
    echo "Repo already cloned, pulling latest..."
    git -C "${REPO_DIR}" pull --ff-only || true
fi

cd "${REPO_DIR}"

# Add upstream remote pointing to the mirror repo (read-only, for syncing with GitHub)
git remote add upstream "${GITEA_URL}/sandbox-admin/${REPO_NAME}.git" 2>/dev/null || true

# Checkout specified branch if set
if [[ -n "${REPO_BRANCH:-}" ]]; then
    git checkout "${REPO_BRANCH}" 2>/dev/null || git checkout -b "${REPO_BRANCH}"
fi

echo "=== Agent container ready ==="
echo "Repo: ${REPO_DIR}"

# Keep the container running
exec tail -f /dev/null
