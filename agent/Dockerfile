FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive

# Core tools: python, node, git, byobu, ssh, utilities
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pip python3-venv \
    nodejs npm \
    git curl wget ca-certificates \
    byobu \
    openssh-server \
    jq \
    sudo \
    locales \
    && rm -rf /var/lib/apt/lists/*

# Locale setup (many tools expect UTF-8)
RUN locale-gen en_US.UTF-8
ENV LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8

# SSH server setup
RUN mkdir -p /run/sshd && \
    sed -i 's/#PermitRootLogin.*/PermitRootLogin no/' /etc/ssh/sshd_config && \
    sed -i 's/#PasswordAuthentication.*/PasswordAuthentication yes/' /etc/ssh/sshd_config && \
    echo "AllowAgentForwarding no" >> /etc/ssh/sshd_config

# Create non-root user for the agent
RUN useradd -m -s /bin/bash -G sudo agent && \
    echo "agent ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/agent

# Workspace directory (will be a Docker volume mount)
RUN mkdir -p /workspace && chown agent:agent /workspace

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

USER agent
WORKDIR /workspace

ENTRYPOINT ["/entrypoint.sh"]
