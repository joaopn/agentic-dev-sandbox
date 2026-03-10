#!/usr/bin/env bash
# Goose setup — runs inside the container at first boot.
# Configures auto-approve mode and headless-friendly settings.

# Ensure ~/.local/bin is in PATH (where Goose installs)
if ! grep -q '.local/bin' ~/.bashrc 2>/dev/null; then
    echo "export PATH=\"\$HOME/.local/bin:\$PATH\"" >> ~/.bashrc
fi

# Pre-configure Goose for headless/autonomous operation (no network needed, only on first run)
if [[ ! -f ~/.config/goose/config.yaml ]]; then
    mkdir -p ~/.config/goose

    # Provider/model are set via env vars at invocation time, not baked into config.
    # This config just sets the operational mode and extensions.
    cat > ~/.config/goose/config.yaml << 'CONFIG'
GOOSE_MODE: "auto"
extensions:
  developer:
    bundled: true
    enabled: true
    timeout: 300
    type: builtin
CONFIG
fi

# Disable keyring — headless containers have no keyring service.
# API keys are passed via environment variables instead.
if ! grep -q 'GOOSE_DISABLE_KEYRING' ~/.bashrc 2>/dev/null; then
    echo 'export GOOSE_DISABLE_KEYRING=true' >> ~/.bashrc
fi
