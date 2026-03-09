#!/usr/bin/env bash
# entrypoint.sh — Agent container entrypoint.
#
# Expected environment variables:
#   GITEA_URL        — Internal Gitea URL (e.g., http://sandbox-gitea:3000)
#   GITEA_TOKEN      — Per-project Gitea API token
#   GITEA_USER       — Per-project Gitea user (e.g., agent-myproject)
#   REPO_NAME        — Repository name
#   BASE_BRANCH      — Base branch for agent work (optional, defaults to repo default)
#   REPO_BRANCH      — Deprecated alias for BASE_BRANCH
#   SSH_PASSWORD     — Password for SSH access
#   HOST_GID         — Host user's GID, for bind-mount access (optional)
#   AGENT_TYPE       — Agent type (e.g. claude, opencode) — triggers setup.sh (optional)

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
export BASE_BRANCH="${BASE_BRANCH:-${REPO_BRANCH:-}}"
export SSH_PASSWORD="${SSH_PASSWORD}"
SANDBOX_ENV

# --- Ensure umask 002 in interactive shells (for host bind-mount access) ---
if ! grep -q 'umask 002' ~/.bashrc 2>/dev/null; then
    echo 'umask 002' >> ~/.bashrc
fi

# --- Agent setup (runs agent-specific configuration from setup.sh) ---
if [[ -f ~/setup.sh ]]; then
    # shellcheck source=/dev/null
    source ~/setup.sh
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

# Resolve base branch: BASE_BRANCH takes priority, REPO_BRANCH is a deprecated alias
EFFECTIVE_BRANCH="${BASE_BRANCH:-${REPO_BRANCH:-}}"

# Checkout specified branch if set
if [[ -n "${EFFECTIVE_BRANCH}" ]]; then
    git checkout "${EFFECTIVE_BRANCH}" 2>/dev/null || git checkout -b "${EFFECTIVE_BRANCH}"
fi

# --- Render template files (replace {{BASE_BRANCH}} placeholders) ---
# Templates are the original files copied from container/; save them for re-rendering.
# Glob covers agent-specific files (CLAUDE.md, AGENTS.md) and universal ones (repo-watch-prompt.md).
for tmpl_file in ~/*.md; do
    [[ -f "${tmpl_file}" ]] || continue
    if grep -q '{{BASE_BRANCH}}' "${tmpl_file}" 2>/dev/null; then
        # Save template as hidden dotfile (e.g. .CLAUDE.md.template) for re-rendering
        tmpl_dir=$(dirname "${tmpl_file}")
        tmpl_base=$(basename "${tmpl_file}")
        cp "${tmpl_file}" "${tmpl_dir}/.${tmpl_base}.template"
        if [[ -n "${EFFECTIVE_BRANCH}" ]]; then
            sed -i "s/{{BASE_BRANCH}}/${EFFECTIVE_BRANCH}/g" "${tmpl_file}"
        else
            # No branch specified — default to repo's current branch
            default_branch=$(git symbolic-ref --short HEAD 2>/dev/null || echo "main")
            sed -i "s/{{BASE_BRANCH}}/${default_branch}/g" "${tmpl_file}"
        fi
    fi
done

# --- Render conditional blocks ({{#CI_WATCH}} / {{^CI_WATCH}} / {{/CI_WATCH}}) ---
for tmpl_file in ~/*.md; do
    [[ -f "${tmpl_file}" ]] || continue
    grep -q '{{#CI_WATCH}}\|{{^CI_WATCH}}' "${tmpl_file}" 2>/dev/null || continue
    if [[ "${CI_WATCH_ENABLED:-}" == "true" ]]; then
        # Keep {{#CI_WATCH}} content, remove {{^CI_WATCH}} content
        sed -i '/{{^CI_WATCH}}/,/{{\/CI_WATCH}}/d' "${tmpl_file}"
        sed -i '/{{#CI_WATCH}}/d; /{{\/CI_WATCH}}/d' "${tmpl_file}"
    else
        # Remove {{#CI_WATCH}} content, keep {{^CI_WATCH}} content
        sed -i '/{{#CI_WATCH}}/,/{{\/CI_WATCH}}/d' "${tmpl_file}"
        sed -i '/{{^CI_WATCH}}/d; /{{\/CI_WATCH}}/d' "${tmpl_file}"
    fi
done

echo "=== Agent container ready ==="
echo "Repo: ${REPO_DIR}"
if [[ -n "${EFFECTIVE_BRANCH}" ]]; then
    echo "Base branch: ${EFFECTIVE_BRANCH}"
fi

# Keep the container running
exec tail -f /dev/null
