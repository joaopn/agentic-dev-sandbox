#!/usr/bin/env bash
# Claude Code setup — runs inside the container at first boot.
# Configures bypass-permissions mode so Claude Code never prompts.

# Ensure ~/.local/bin is in PATH (where Claude Code installs)
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
