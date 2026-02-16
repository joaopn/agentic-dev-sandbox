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

set -euo pipefail

echo "=== Agent container starting ==="

# --- Restore home directory dotfiles (volume mount hides image contents) ---
sudo chown agent:agent /home/agent
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
REPO_DIR=~/"${REPO_NAME}"

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

# --- Start byobu session ---
echo "Starting byobu session 'main'..."
byobu new-session -d -s main -c /home/agent "exec bash"

echo "=== Agent container ready ==="
echo "Repo: ${REPO_DIR}"
echo "Byobu session: main"

# Keep the container running
exec tail -f /dev/null
