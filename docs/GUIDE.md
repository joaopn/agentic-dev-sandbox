# Guide

## After a Reboot

```bash
docker compose up -d           # Gitea, router, review service
python sandbox.py start --all  # Agent containers
```

To fully tear down everything (all agent containers, volumes, networks, and infrastructure):

```bash
python sandbox.py unsetup
```

This removes all agent containers and their workspace volumes, stops and removes Gitea/router/review
containers and their Docker volumes, cleans up per-project networks, and removes the generated
`GITEA_ADMIN_TOKEN` from `.env`. Your other `.env` settings are preserved — run `sandbox setup` to
start fresh.

## Image Profiles

Profiles let you pick different base environments for agent containers. Each profile is a
Dockerfile in `agent/` named `Dockerfile.<profile>`.

| Profile | Base image | Includes |
|---|---|---|
| `python` | `continuumio/miniconda3` | Conda, git, byobu, sshd, jq, nano |

```bash
python sandbox.py create https://github.com/you/myproject --profile python
```

To add a custom profile, create `agent/Dockerfile.myprofile`. Copy an existing Dockerfile
as a starting point. The entrypoint is always `agent/entrypoint.sh`.

**Structural requirements:**

- Non-root `agent` user with UID 1000 and passwordless sudo
- `/home/agent` as the working directory
- Home directory contents stashed to `/etc/agent-skel` (the entrypoint copies them into
  the workspace volume on first run)

**Required packages:**

| Package | Needed by |
|---|---|
| `git` | entrypoint (clone, remotes), all git workflows |
| `openssh-server` | entrypoint (sshd), VS Code Remote-SSH |
| `byobu` | entrypoint (terminal session) |
| `sudo` | entrypoint (user setup) |
| `curl` | repo-watch (Gitea API) |
| `jq` | repo-watch (JSON parsing) |

**Recommended:**

| Package | Why |
|---|---|
| `locales` | UTF-8 support — without it, some tools misbehave on non-ASCII content |
| `nano` | Lightweight editor for quick in-container edits |
| `iputils-ping` | Useful for debugging network isolation |

## `container/` Directory

Any files placed in `container/` are copied into each agent's home directory volume at `/home/agent/`.
Use this to provide config files or custom instructions to every agent. For key-based SSH access,
place your public key at `container/.ssh/authorized_keys`.

By default it ships with:
- `CLAUDE.md` — baseline agent instructions (git workflow, remotes, verification)
- `repo-watch.sh` — agentic loop script (see [Repo Watch](#repo-watch))
- `repo-watch-prompt.md` — prompt template for repo-watch

## Git Remotes Inside the Container

Each agent container has two git remotes:
- **`origin`** — the agent's fork on Gitea (read-write)
- **`upstream`** — the mirror of the GitHub repo (read-only)

After you merge the agent's work on GitHub and the mirror syncs, the agent must
`git fetch upstream && git merge upstream/main` before starting new work. This is
**not done automatically** — it is up to the agent (or the user) to sync. The default
`CLAUDE.md` instructs Claude Code to do this before each task.

## VS Code Remote-SSH

Connect to agent containers via VS Code Remote-SSH for full IDE access:

```bash
sandbox ssh   # shows port + password for all containers
ssh agent@localhost -p <ssh-port>
```

Once connected, run `byobu attach` in the VS Code terminal to connect to the
agent session. Displaying the password is not a security issue, as anyone with docker permissions
can already connect directly to the agent container.

**Important**: Verify these VS Code settings are disabled before connecting:
- `remote.SSH.enableAgentForwarding` — must be off (forwards host SSH keys)
- Git credential forwarding — must not be configured

## Reviewer Configuration

The review service posts automated security reviews as Gitea commit comments. It
supports multiple LLM providers and can be disabled entirely.

Runtime settings go in `.env`, while the review prompt and default provider
endpoints live in `review/review-config.yaml`.

**Env vars** (`.env`):

| Variable | Description | Default |
|---|---|---|
| `REVIEWER_ENABLED` | Enable automated reviews (`true`/`false`) | `false` |
| `REVIEWER_PROVIDER` | LLM provider: `anthropic`, `openai`, `openrouter`, `local` | `anthropic` |
| `REVIEWER_API_KEY` | API key for the provider | (required unless local) |
| `REVIEWER_MODEL` | Model name | (required) |
| `REVIEWER_ENDPOINT` | Custom API endpoint (overrides config yaml) | from config yaml |

Default endpoints per provider are in `review/review-config.yaml`. The `local`
provider has no default — `REVIEWER_ENDPOINT` is required.

**Examples:**

```bash
# Disable reviews entirely
REVIEWER_ENABLED=false

# Use OpenAI
REVIEWER_PROVIDER=openai
REVIEWER_API_KEY=sk-xxxx
REVIEWER_MODEL=gpt-4o

# Use a local vLLM instance
REVIEWER_PROVIDER=local
REVIEWER_MODEL=meta-llama/Llama-3.1-70B-Instruct
REVIEWER_ENDPOINT=http://192.168.1.50:8000
```

When `REVIEWER_ENABLED=false`, the review service container is not started
and no webhooks are created on agent repos.

**Customizing the review prompt:** Edit `review/review-config.yaml`. The prompt
must contain a `{diff}` placeholder. Rebuild the review container after changes:
`docker compose build review && docker compose up -d review`.

## Repo Watch

`repo-watch.sh` is a bash script that turns the agent into an autonomous developer you
interact with through Gitea issues. It polls the Gitea API, detects new activity, and
invokes Claude Code to handle it.

### How it works

The script checks open issues assigned to the agent every `POLL_INTERVAL` seconds
(default: 30). For each issue:

- If the last comment is by the agent — skip (waiting for human input).
- If the last comment is by a human (or the issue is new) — invoke Claude Code with
  the full conversation context.

It also monitors open PRs authored by the agent for line-level review comments that
don't show up as issue comments.

One issue is processed per cycle. The script blocks the terminal — use byobu F2
for a new window.

### Usage

```bash
./repo-watch.sh                        # poll every 30s (default)
POLL_INTERVAL=60 ./repo-watch.sh       # poll every 60s
```

### Customizing the prompt

Edit `~/repo-watch-prompt.md` inside the container. This file contains the agent's
behavioral instructions and Gitea API examples. The script appends the issue context
to the end of this file before invoking Claude Code.

### Labels

On first run, the script creates these labels (idempotent):
- `in-progress` — agent is working on it
- `needs-review` — agent opened a PR
- `done` — merged and closed

### Retry behavior

If Claude Code errors out, the script retries up to `MAX_RETRIES` times (default: 3)
before skipping that issue. A new human comment resets the counter.

## FAQ

### Why not use dev containers?

Dev containers were designed to give you a reproducible dev environment, not to isolate an untrusted agent.
By default they bind-mount your project directory (read-write), share the host network, and have no egress filtering.
The agent can read your `.git/config`, reach `localhost` services, and access anything in the mounted tree.

### Can't I just harden the dev container?

The IDE works against you.
VS Code (for instance) automatically forwards your SSH agent, git credentials, and GPG keys into the container.
Extensions run with full container permissions.
An update can re-enable unhardened defaults.

### GPU / CUDA support?

Install the [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html) on the host and pass `--gpus all` when creating the project. Create a custom profile with a CUDA base image if your workload needs one — the toolkit mounts the host driver automatically.

### Rootless Docker support?

If you already run [rootless Docker](https://docs.docker.com/engine/security/rootless/), the sandbox works as-is with no changes.
The added benefit is that a container escape lands as your unprivileged user rather than root, and granted capabilities (CHOWN,SETUID, etc.) are scoped to a user namespace that can't affect the real host.
This doesn't prevent the escape itself, but limits the blast radius.
Not required — regular Docker with the network isolation above is the intended baseline.
